# Copyright (C) 2016 iNuron NV
#
# This file is part of Open vStorage Open Source Edition (OSE),
# as available from
#
#      http://www.openvstorage.org and
#      http://www.openvstorage.com.
#
# This file is free software; you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License v3 (GNU AGPLv3)
# as published by the Free Software Foundation, in version 3 as it comes
# in the LICENSE.txt file of the Open vStorage OSE distribution.
#
# Open vStorage is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY of any kind.

"""
StorageRouter module
"""
import os
import copy
import json
import time
from ConfigParser import RawConfigParser
from subprocess import CalledProcessError
from StringIO import StringIO
from ovs.celery_run import celery
from ovs.dal.hybrids.disk import Disk
from ovs.dal.hybrids.diskpartition import DiskPartition
from ovs.dal.hybrids.j_albaproxy import AlbaProxy
from ovs.dal.hybrids.j_storagedriverpartition import StorageDriverPartition
from ovs.dal.hybrids.service import Service as DalService
from ovs.dal.hybrids.servicetype import ServiceType
from ovs.dal.hybrids.storagedriver import StorageDriver
from ovs.dal.hybrids.storagerouter import StorageRouter
from ovs.dal.hybrids.vdisk import VDisk
from ovs.dal.hybrids.vpool import VPool
from ovs.dal.lists.servicetypelist import ServiceTypeList
from ovs.dal.lists.storagedriverlist import StorageDriverList
from ovs.dal.lists.storagerouterlist import StorageRouterList
from ovs.dal.lists.vdisklist import VDiskList
from ovs.dal.lists.vpoollist import VPoolList
from ovs.extensions.api.client import OVSClient
from ovs.extensions.db.arakoon.ArakoonInstaller import ArakoonClusterConfig, ArakoonInstaller
from ovs.extensions.generic.configuration import Configuration
from ovs.extensions.generic.disk import DiskTools
from ovs.extensions.generic.remote import remote
from ovs.extensions.generic.sshclient import SSHClient, UnableToConnectException
from ovs.extensions.generic.system import System
from ovs.extensions.generic.volatilemutex import volatile_mutex
from ovs.extensions.packages.package import PackageManager
from ovs.extensions.services.service import ServiceManager
from ovs.extensions.storageserver.storagedriver import StorageDriverConfiguration, StorageDriverClient
from ovs.extensions.support.agent import SupportAgent
from ovs.lib.disk import DiskController
from ovs.lib.helpers.decorators import add_hooks, ensure_single
from ovs.lib.helpers.toolbox import Toolbox
from ovs.lib.mdsservice import MDSServiceController
from ovs.lib.storagedriver import StorageDriverController
from ovs.lib.vdisk import VDiskController
from ovs.log.log_handler import LogHandler
from volumedriver.storagerouter import storagerouterclient
from volumedriver.storagerouter.storagerouterclient import ArakoonNodeConfig, ClusterNodeConfig, ClusterRegistry, LocalStorageRouterClient


class StorageRouterController(object):
    """
    Contains all BLL related to StorageRouter
    """
    _logger = LogHandler.get('lib', name='storagerouter')
    SUPPORT_AGENT = 'support-agent'

    storagerouterclient.Logger.setupLogging(LogHandler.load_path('storagerouterclient'))
    # noinspection PyArgumentList
    storagerouterclient.Logger.enableLogging()

    @staticmethod
    @celery.task(name='ovs.storagerouter.ping')
    def ping(storagerouter_guid, timestamp):
        """
        Update a Storage Router's celery heartbeat
        :param storagerouter_guid: Guid of the Storage Router to update
        :type storagerouter_guid: str
        :param timestamp: Timestamp to compare to
        :type timestamp: float
        """
        with volatile_mutex('storagerouter_heartbeat_{0}'.format(storagerouter_guid)):
            storagerouter = StorageRouter(storagerouter_guid)
            if timestamp > storagerouter.heartbeats.get('celery', 0):
                storagerouter.heartbeats['celery'] = timestamp
                storagerouter.save()

    @staticmethod
    @celery.task(name='ovs.storagerouter.get_metadata')
    def get_metadata(storagerouter_guid):
        """
        Gets physical information about the machine this task is running on
        :param storagerouter_guid: Storage Router guid to retrieve the metadata for
        :type storagerouter_guid: str
        :return: Metadata information about the Storage Router
        :rtype: dict
        """
        storagerouter = StorageRouter(storagerouter_guid)
        client = SSHClient(storagerouter)
        ipaddresses = client.run("ip a | grep 'inet ' | sed 's/\s\s*/ /g' | cut -d ' ' -f 3 | cut -d '/' -f 1", allow_insecure=True).strip().splitlines()
        ipaddresses = [ipaddr.strip() for ipaddr in ipaddresses if ipaddr.strip() != '127.0.0.1']
        services_mds = ServiceTypeList.get_by_name(ServiceType.SERVICE_TYPES.MD_SERVER).services
        services_arakoon = [service for service in ServiceTypeList.get_by_name(ServiceType.SERVICE_TYPES.ARAKOON).services if service.name != 'arakoon-ovsdb' and service.is_internal is True]

        partitions = dict((role, []) for role in DiskPartition.ROLES)
        writecache_size = 0
        for disk in storagerouter.disks:
            for disk_partition in disk.partitions:
                claimed_space_by_fwk = 0
                used_space_by_system = 0
                available_space_by_system = 0
                for storagedriver_partition in disk_partition.storagedrivers:
                    claimed_space_by_fwk += storagedriver_partition.size if storagedriver_partition.size is not None else 0
                    if client.dir_exists(storagedriver_partition.path):
                        try:
                            used_space_by_system += int(client.run('du -B 1 -d 0 {0}'.format(storagedriver_partition.path)).split('\t')[0])
                        except Exception as ex:
                            StorageRouterController._logger.warning('Failed to get directory usage for {0}. {1}'.format(storagedriver_partition.path, ex))

                if disk_partition.mountpoint is not None:
                    for alias in disk_partition.aliases:
                        StorageRouterController._logger.info('Verifying disk partition usage by checking path {0}'.format(alias))
                        disk_partition_device = client.file_read_link(path=alias)
                        try:
                            available_space_by_system = int(client.run('df -B 1 --output=avail {0}'.format(disk_partition_device)).splitlines()[-1])
                            break
                        except Exception as ex:
                            StorageRouterController._logger.warning('Failed to get partition usage for {0}. {1}'.format(disk_partition.mountpoint, ex))

                for role in disk_partition.roles:
                    size = 0 if disk_partition.size is None else disk_partition.size
                    if available_space_by_system > 0:
                        # Take available space reported by df then add back used by roles so that the only used space reported is space not managed by us
                        available = available_space_by_system + used_space_by_system - claimed_space_by_fwk
                    else:
                        available = size - claimed_space_by_fwk  # Subtract size for roles which have already been claimed by other vpools (but not necessarily already been fully used)

                    if available > 0:
                        if role == DiskPartition.ROLES.WRITE:
                            writecache_size += available
                    else:
                        available = 0

                    in_use = any(junction for junction in disk_partition.storagedrivers if junction.role == role)
                    if role == DiskPartition.ROLES.DB:
                        for service in services_arakoon:
                            if service.storagerouter_guid == storagerouter_guid:
                                in_use = True
                                break
                        for service in services_mds:
                            if service.storagerouter_guid == storagerouter_guid:
                                in_use = True
                                break

                    partitions[role].append({'ssd': disk.is_ssd,
                                             'guid': disk_partition.guid,
                                             'size': size,
                                             'in_use': in_use,
                                             'available': available,
                                             'mountpoint': disk_partition.folder,  # Equals to mountpoint unless mountpoint is root ('/'), then we pre-pend mountpoint with '/mnt/storage'
                                             'storagerouter_guid': storagerouter_guid})

        return {'partitions': partitions,
                'ipaddresses': ipaddresses,
                'writecache_size': writecache_size,
                'scrub_available': StorageRouterController._check_scrub_partition_present()}

    @staticmethod
    @celery.task(name='ovs.storagerouter.add_vpool')
    def add_vpool(parameters):
        """
        Add a vPool to the machine this task is running on
        :param parameters: Parameters for vPool creation
        :type parameters: dict
        :return: None
        """
        required_params = {'vpool_name': (str, Toolbox.regex_vpool),
                           'storage_ip': (str, Toolbox.regex_ip),
                           'storagerouter_ip': (str, Toolbox.regex_ip),
                           'writecache_size': (int, {'min': 1, 'max': 10240}),
                           'config_params': (dict, {'dtl_mode': (str, StorageDriverClient.VPOOL_DTL_MODE_MAP.keys()),
                                                    'sco_size': (int, StorageDriverClient.TLOG_MULTIPLIER_MAP.keys()),
                                                    'cluster_size': (int, StorageDriverClient.CLUSTER_SIZES),
                                                    'write_buffer': (int, {'min': 128, 'max': 10240}),
                                                    'dtl_transport': (str, StorageDriverClient.VPOOL_DTL_TRANSPORT_MAP.keys())}),
                           'fragment_cache_on_read': (bool, None),
                           'fragment_cache_on_write': (bool, None),
                           'backend_info': (dict, {'preset': (str, Toolbox.regex_preset),
                                                   'alba_backend_guid': (str, Toolbox.regex_guid)}),
                           'backend_info_aa': (dict, {'preset': (str, Toolbox.regex_preset),
                                                      'alba_backend_guid': (str, Toolbox.regex_guid)}, False),
                           'connection_info': (dict, {'host': (str, Toolbox.regex_ip),
                                                      'port': (int, None),
                                                      'client_id': (str, None),
                                                      'client_secret': (str, None),
                                                      'local': (bool, None, False)})}

        ########################
        # VALIDATIONS (PART 1) #
        ########################

        # Check parameters
        if not isinstance(parameters, dict):
            raise ValueError('Parameters should be of type "dict"')
        Toolbox.verify_required_params(required_params, parameters)

        client = SSHClient(parameters['storagerouter_ip'])
        unique_id = System.get_my_machine_id(client)

        sd_config_params = parameters['config_params']
        sco_size = sd_config_params['sco_size']
        write_buffer = sd_config_params['write_buffer']
        if (sco_size == 128 and write_buffer < 256) or not (128 <= write_buffer <= 10240):
            raise ValueError('Incorrect storagedriver configuration settings specified')

        # Verify vPool status and additional parameters
        vpool_name = parameters['vpool_name']
        vpool = VPoolList.get_vpool_by_name(vpool_name)
        new_vpool = vpool is None
        if new_vpool is False:
            if vpool.status != VPool.STATUSES.RUNNING:
                raise ValueError('VPool should be in {0} status'.format(VPool.STATUSES.RUNNING))

        # Check storagerouter existence
        storagerouter = StorageRouterList.get_by_ip(client.ip)
        if storagerouter is None:
            raise RuntimeError('Could not find Storage Router with given IP address {0}'.format(client.ip))

        # Check duplicate vPool StorageDriver
        all_storagerouters = [storagerouter]
        if new_vpool is False:
            required_params_sd_config = {'sco_size': (int, StorageDriverClient.TLOG_MULTIPLIER_MAP.keys()),
                                         'dtl_mode': (str, StorageDriverClient.VPOOL_DTL_MODE_MAP.keys()),
                                         'write_buffer': (float, None),
                                         'dtl_transport': (str, StorageDriverClient.VPOOL_DTL_TRANSPORT_MAP.keys()),
                                         'tlog_multiplier': (int, StorageDriverClient.TLOG_MULTIPLIER_MAP.values())}
            Toolbox.verify_required_params(required_params=required_params_sd_config,
                                           actual_params=vpool.configuration)

            for vpool_storagedriver in vpool.storagedrivers:
                if vpool_storagedriver.storagerouter_guid == storagerouter.guid:
                    raise RuntimeError('A Storage Driver is already linked to this Storage Router for this vPool: {0}'.format(vpool_name))
            all_storagerouters += [sd.storagerouter for sd in vpool.storagedrivers]

        # Check storagerouter connectivity
        ip_client_map = {}
        offline_nodes_detected = False
        for sr in all_storagerouters:
            try:
                ip_client_map[sr.ip] = {'ovs': SSHClient(sr.ip, username='ovs'),
                                        'root': SSHClient(sr.ip, username='root')}
            except UnableToConnectException:
                offline_nodes_detected = True  # We currently want to allow offline nodes while setting up or extend a vpool (etcd implementation should prevent further failures)
            except Exception as ex:
                raise RuntimeError('Something went wrong building SSH connections. {0}'.format(ex))

        ################
        # CREATE VPOOL #
        ################
        connection_info = parameters['connection_info']
        if new_vpool is True:
            vpool = VPool()
            vpool.name = vpool_name
            vpool.login = connection_info['client_id']
            vpool.password = connection_info['client_secret']
            vpool.metadata = {}
            vpool.connection = '{0}:{1}'.format(connection_info['host'], connection_info['port'])
            vpool.description = vpool_name
            vpool.rdma_enabled = sd_config_params['dtl_transport'] == StorageDriverClient.FRAMEWORK_DTL_TRANSPORT_RSOCKET
            vpool.status = VPool.STATUSES.INSTALLING
            vpool.save()
        else:
            vpool.status = VPool.STATUSES.EXTENDING
            vpool.save()

        ########################
        # VALIDATIONS (PART 2) #
        ########################
        # Check mountpoint
        error_messages = []
        if StorageRouterController.mountpoint_exists(name=vpool_name, storagerouter_guid=storagerouter.guid):
            error_messages.append('The mountpoint for vPool {0} already exists'.format(vpool_name))

        # Check required roles
        metadata = StorageRouterController.get_metadata(storagerouter.guid)
        partition_info = metadata['partitions']
        required_roles = [DiskPartition.ROLES.WRITE, DiskPartition.ROLES.DB]
        for required_role in required_roles:
            if required_role not in partition_info:
                error_messages.append('Missing required partition role {0}'.format(required_role))
            elif len(partition_info[required_role]) == 0:
                error_messages.append('At least 1 {0} partition role is required'.format(required_role))
            else:
                total_available = [part['available'] for part in partition_info[required_role]]
                if total_available == 0:
                    error_messages.append('Not enough available space for {0}'.format(required_role))

        # Check backend information and connection information
        backend_info = parameters['backend_info']
        backend_info_aa = parameters.get('backend_info_aa', {})
        alba_backend_guid = backend_info['alba_backend_guid']
        alba_backend_guid_aa = backend_info_aa.get('alba_backend_guid')
        connection_info_aa = parameters.get('connection_info_aa', {})
        use_accelerated_alba = alba_backend_guid_aa is not None
        if alba_backend_guid == alba_backend_guid_aa:
            error_messages.append('Backend and accelerated backend cannot be the same')
        if alba_backend_guid_aa is not None:
            if 'connection_info_aa' not in parameters:
                error_messages.append('Missing the connection information for the accelerated Backend')
            else:
                try:
                    Toolbox.verify_required_params(required_params={'host': (str, Toolbox.regex_ip),
                                                                    'port': (int, None),
                                                                    'client_id': (str, None),
                                                                    'client_secret': (str, None),
                                                                    'local': (bool, None, False)},
                                                   actual_params=connection_info_aa)
                except RuntimeError as rte:
                    error_messages.append(rte.message)

        # Check mountpoints are mounted
        for role, part_info in partition_info.iteritems():
            for part in part_info:
                if not client.is_mounted(part['mountpoint']) and part['mountpoint'] != DiskPartition.VIRTUAL_STORAGE_LOCATION:
                    error_messages.append('Mountpoint {0} is not mounted'.format(part['mountpoint']))

        # Check over-allocation for write cache
        writecache_size_available = metadata['writecache_size']
        writecache_size_requested = parameters['writecache_size'] * 1024 ** 3
        if writecache_size_requested > writecache_size_available:
            error_messages.append('Too much space requested for {0} cache. Available: {1:.2f} GiB, Requested: {2:.2f} GiB'.format(DiskPartition.ROLES.WRITE,
                                                                                                                                  writecache_size_available / 1024.0 ** 3,
                                                                                                                                  writecache_size_requested / 1024.0 ** 3))

        if metadata['scrub_available'] is False:
            error_messages.append('At least 1 Storage Router must have a {0} partition'.format(DiskPartition.ROLES.SCRUB))

        # Check DB role
        arakoon_service_found = False
        for service in ServiceTypeList.get_by_name(ServiceType.SERVICE_TYPES.ARAKOON).services:
            if service.name == 'arakoon-voldrv':
                arakoon_service_found = True
                break
        if arakoon_service_found is False and (DiskPartition.ROLES.DB not in partition_info or len(partition_info[DiskPartition.ROLES.DB]) == 0):
            error_messages.append('DB partition role required')

        # Check current vPool configuration
        if new_vpool is False:
            current_vpool_configuration = vpool.configuration
            for key in sd_config_params.keys():
                current_value = current_vpool_configuration.get(key)
                specified_value = sd_config_params[key]
                if specified_value != current_value:
                    error_messages.append('Specified StorageDriver config "{0}" with value {1} does not match the value {2}'.format(key, specified_value, current_value))

        if error_messages:
            if new_vpool is True:
                vpool.delete()
            else:
                vpool.status = VPool.STATUSES.FAILURE
                vpool.save()
            raise ValueError('Errors validating the specified parameters:\n - {0}'.format('\n - '.join(set(error_messages))))

        ########################
        # RENEW VPOOL METADATA #
        ########################
        StorageRouterController._logger.info('Add vPool {0} started'.format(vpool_name))
        if new_vpool is True:
            metadata_map = {'backend': {'backend_info': backend_info,
                                        'connection_info': connection_info}}
        else:
            metadata_map = copy.deepcopy(vpool.metadata)

        if use_accelerated_alba is True:
            metadata_map['backend_aa_{0}'.format(storagerouter.guid)] = {'backend_info': backend_info_aa,
                                                                         'connection_info': connection_info_aa}

        fragment_cache_on_read = parameters['fragment_cache_on_read']
        fragment_cache_on_write = parameters['fragment_cache_on_write']
        for key, metadata in metadata_map.iteritems():
            ovs_client = OVSClient(ip=metadata['connection_info']['host'],
                                   port=metadata['connection_info']['port'],
                                   credentials=(metadata['connection_info']['client_id'], metadata['connection_info']['client_secret']),
                                   version=2)
            preset_name = metadata['backend_info']['preset']
            alba_backend_guid = metadata['backend_info']['alba_backend_guid']
            arakoon_config = StorageRouterController._retrieve_alba_arakoon_config(backend_guid=alba_backend_guid, ovs_client=ovs_client)
            try:
                backend_dict = ovs_client.get('/alba/backends/{0}/'.format(alba_backend_guid), params={'contents': 'name,usages,presets,backend'})
                preset_info = dict((preset['name'], preset) for preset in backend_dict['presets'])
                if preset_name not in preset_info:
                    raise RuntimeError('Given preset {0} is not available in backend {1}'.format(preset_name, backend_dict['name']))
            except:
                if new_vpool is True:
                    vpool.delete()
                    raise
                vpool.status = VPool.STATUSES.FAILURE
                vpool.save()
                raise

            policies = []
            for policy_info in preset_info[preset_name]['policies']:
                policy = json.loads('[{0}]'.format(policy_info.strip('()')))
                policies.append([policy[0], policy[1]])

            if key in vpool.metadata:
                vpool.metadata[key]['backend_info']['policies'] = policies
                vpool.metadata[key]['arakoon_config'] = arakoon_config
            else:
                vpool.metadata[key] = {'backend_info': {'name': backend_dict['name'],
                                                        'preset': preset_name,
                                                        'policies': policies,
                                                        'sco_size': sco_size * 1024.0 ** 2 if new_vpool is True else vpool.configuration['sco_size'] * 1024.0 ** 2,
                                                        'frag_size': float(preset_info[preset_name]['fragment_size']),
                                                        'total_size': float(backend_dict['usages']['size']),
                                                        'backend_guid': backend_dict['backend_guid'],
                                                        'alba_backend_guid': alba_backend_guid},
                                       'connection_info': metadata['connection_info'],
                                       'arakoon_config': arakoon_config}
        if 'caching_info' not in vpool.metadata['backend']:
            vpool.metadata['backend']['caching_info'] = {}
        vpool.metadata['backend']['caching_info'][storagerouter.guid] = {'fragment_cache_on_read': fragment_cache_on_read,
                                                                         'fragment_cache_on_write': fragment_cache_on_write}
        vpool.save()

        ####################################
        # ARAKOON SETUP AND CONFIGURATIONS #
        ####################################
        if arakoon_service_found is False:
            StorageDriverController.manual_voldrv_arakoon_checkup()

        # Verify SD arakoon cluster is available and 'in_use'
        root_client = ip_client_map[storagerouter.ip]['root']
        watcher_volumedriver_service = 'watcher-volumedriver'
        if not ServiceManager.has_service(watcher_volumedriver_service, client=root_client):
            ServiceManager.add_service(watcher_volumedriver_service, client=root_client)
            ServiceManager.start_service(watcher_volumedriver_service, client=root_client)

        model_ports_in_use = []
        for port_storagedriver in StorageDriverList.get_storagedrivers():
            if port_storagedriver.storagerouter_guid == storagerouter.guid:
                # Local storagedrivers
                model_ports_in_use += port_storagedriver.ports.values()
                if port_storagedriver.alba_proxy is not None:
                    model_ports_in_use.append(port_storagedriver.alba_proxy.service.ports[0])

        # Connection information is Storage Driver related information
        ports = StorageRouterController._get_free_ports(client, model_ports_in_use, 4)
        model_ports_in_use += ports

        # Prepare the model
        vrouter_id = '{0}{1}'.format(vpool_name, unique_id)
        storagedriver = StorageDriver()
        storagedriver.name = vrouter_id.replace('_', ' ')
        storagedriver.ports = {'management': ports[0],
                               'xmlrpc': ports[1],
                               'dtl': ports[2],
                               'edge': ports[3]}
        storagedriver.vpool = vpool
        storagedriver.cluster_ip = Configuration.get('/ovs/framework/hosts/{0}/ip'.format(unique_id))
        storagedriver.storage_ip = parameters['storage_ip']
        storagedriver.mountpoint = '/mnt/{0}'.format(vpool_name)
        storagedriver.description = storagedriver.name
        storagedriver.storagerouter = storagerouter
        storagedriver.storagedriver_id = vrouter_id

        arakoon_cluster_name = str(Configuration.get('/ovs/framework/arakoon_clusters|voldrv'))
        config = ArakoonClusterConfig(cluster_id=arakoon_cluster_name, filesystem=False)
        config.load_config()
        arakoon_nodes = []
        arakoon_node_configs = []
        for node in config.nodes:
            arakoon_nodes.append({'node_id': node.name, 'host': node.ip, 'port': node.client_port})
            arakoon_node_configs.append(ArakoonNodeConfig(str(node.name), str(node.ip), node.client_port))
        node_configs = []
        for existing_storagedriver in StorageDriverList.get_storagedrivers():
            if existing_storagedriver.vpool_guid == vpool.guid:
                existing_storagedriver.invalidate_dynamics('cluster_node_config')
                node_configs.append(ClusterNodeConfig(**existing_storagedriver.cluster_node_config))
        storagedriver.invalidate_dynamics('cluster_node_config')
        node_configs.append(ClusterNodeConfig(**storagedriver.cluster_node_config))

        try:
            vrouter_clusterregistry = ClusterRegistry(str(vpool.guid), arakoon_cluster_name, arakoon_node_configs)
            vrouter_clusterregistry.set_node_configs(node_configs)
        except:
            vpool.status = VPool.STATUSES.FAILURE
            vpool.save()
            if new_vpool is True:
                vpool.delete()
            raise

        # Store the model
        storagedriver.save()

        ##############################
        # CREATE PARTITIONS IN MODEL #
        ##############################

        # Retrieve largest write mountpoint (SSD > SATA). We need largest SSD to put fragment cache on
        largest_ssd_write_mountpoint = None
        largest_sata_write_mountpoint = None
        largest_ssd = 0
        largest_sata = 0
        for role, info in partition_info.iteritems():
            if role == DiskPartition.ROLES.WRITE:
                for part in info:
                    if part['ssd'] is True and part['available'] > largest_ssd:
                        largest_ssd = part['available']
                        largest_ssd_write_mountpoint = part['guid']
                    elif part['ssd'] is False and part['available'] > largest_sata:
                        largest_sata = part['available']
                        largest_sata_write_mountpoint = part['guid']

        largest_write_mountpoint = DiskPartition(largest_ssd_write_mountpoint or largest_sata_write_mountpoint or partition_info[DiskPartition.ROLES.WRITE][0]['guid'])
        mountpoint_fragment_cache = None
        if use_accelerated_alba is False:
            mountpoint_fragment_cache = largest_write_mountpoint

        # Calculate WRITE / FRAG cache
        frag_size = None
        sdp_frag = None
        dirs2create = list()
        writecaches = list()
        writecache_information = partition_info[DiskPartition.ROLES.WRITE]
        total_available = sum([part['available'] for part in writecache_information])
        for writecache_info in writecache_information:
            available = writecache_info['available']
            partition = DiskPartition(writecache_info['guid'])
            proportion = available * 100.0 / total_available
            size_to_be_used = proportion * writecache_size_requested / 100
            if mountpoint_fragment_cache is not None and partition == mountpoint_fragment_cache:
                frag_size = int(size_to_be_used * 0.10)  # Bytes
                w_size = int(size_to_be_used * 0.88 / 1024 / 4096) * 4096  # KiB
                sdp_frag = StorageDriverController.add_storagedriverpartition(storagedriver, {'size': None,
                                                                                              'role': DiskPartition.ROLES.WRITE,
                                                                                              'sub_role': StorageDriverPartition.SUBROLE.FCACHE,
                                                                                              'partition': DiskPartition(writecache_info['guid'])})
                sdp_write = StorageDriverController.add_storagedriverpartition(storagedriver, {'size': long(size_to_be_used),
                                                                                               'role': DiskPartition.ROLES.WRITE,
                                                                                               'sub_role': StorageDriverPartition.SUBROLE.SCO,
                                                                                               'partition': DiskPartition(writecache_info['guid'])})
                dirs2create.append(sdp_frag.path)
            else:
                w_size = int(size_to_be_used * 0.98 / 1024 / 4096) * 4096
                sdp_write = StorageDriverController.add_storagedriverpartition(storagedriver, {'size': long(size_to_be_used),
                                                                                               'role': DiskPartition.ROLES.WRITE,
                                                                                               'sub_role': StorageDriverPartition.SUBROLE.SCO,
                                                                                               'partition': DiskPartition(writecache_info['guid'])})
            writecaches.append({'path': sdp_write.path,
                                'size': '{0}KiB'.format(w_size)})
            dirs2create.append(sdp_write.path)

        # Assign DB
        db_info = partition_info[DiskPartition.ROLES.DB][0]
        sdp_tlogs = StorageDriverController.add_storagedriverpartition(storagedriver, {'size': None,
                                                                                       'role': DiskPartition.ROLES.DB,
                                                                                       'sub_role': StorageDriverPartition.SUBROLE.TLOG,
                                                                                       'partition': DiskPartition(db_info['guid'])})
        sdp_metadata = StorageDriverController.add_storagedriverpartition(storagedriver, {'size': None,
                                                                                          'role': DiskPartition.ROLES.DB,
                                                                                          'sub_role': StorageDriverPartition.SUBROLE.MD,
                                                                                          'partition': DiskPartition(db_info['guid'])})
        dirs2create.append(sdp_tlogs.path)
        dirs2create.append(sdp_metadata.path)

        sdp_fd = StorageDriverController.add_storagedriverpartition(storagedriver, {'size': None,
                                                                                    'role': DiskPartition.ROLES.WRITE,
                                                                                    'sub_role': StorageDriverPartition.SUBROLE.FD,
                                                                                    'partition': largest_write_mountpoint})
        sdp_dtl = StorageDriverController.add_storagedriverpartition(storagedriver, {'size': None,
                                                                                     'role': DiskPartition.ROLES.WRITE,
                                                                                     'sub_role': StorageDriverPartition.SUBROLE.DTL,
                                                                                     'partition': largest_write_mountpoint})
        dirs2create.append(sdp_dtl.path)
        dirs2create.append(sdp_fd.path)
        dirs2create.append(storagedriver.mountpoint)

        if frag_size is None and use_accelerated_alba is False:
            vpool.status = VPool.STATUSES.FAILURE
            vpool.save()
            raise ValueError('Something went wrong trying to calculate the fragment cache size')

        root_client.dir_create(dirs2create)

        ############################
        # CONFIGURATION MANAGEMENT #
        ############################
        config_dir = '{0}/storagedriver/storagedriver'.format(Configuration.get('/ovs/framework/paths|cfgdir'))
        client.dir_create(config_dir)
        alba_proxy = storagedriver.alba_proxy
        manifest_cache_size = 16 * 1024 * 1024 * 1024
        if alba_proxy is None:
            service = DalService()
            service.storagerouter = storagerouter
            service.ports = [StorageRouterController._get_free_ports(client, model_ports_in_use, 1)]
            service.name = 'albaproxy_{0}'.format(vpool_name)
            service.type = ServiceTypeList.get_by_name(ServiceType.SERVICE_TYPES.ALBA_PROXY)
            service.save()
            alba_proxy = AlbaProxy()
            alba_proxy.service = service
            alba_proxy.storagedriver = storagedriver
            alba_proxy.save()

            config_tree = '/ovs/vpools/{0}/proxies/{1}/config/{{0}}'.format(vpool.guid, alba_proxy.guid)
            metadata_keys = {'backend': 'abm'} if use_accelerated_alba is False else {'backend': 'abm',
                                                                                      'backend_aa_{0}'.format(storagerouter.guid): 'abm_aa'}
            for metadata_key in metadata_keys:
                arakoon_config = vpool.metadata[metadata_key]['arakoon_config']
                config = RawConfigParser()
                for section in arakoon_config:
                    config.add_section(section)
                    for key, value in arakoon_config[section].iteritems():
                        config.set(section, key, value)
                config_io = StringIO()
                config.write(config_io)
                Configuration.set(config_tree.format(metadata_keys[metadata_key]), config_io.getvalue(), raw=True)

            if fragment_cache_on_read is False and fragment_cache_on_write is False:
                fragment_cache_info = ['none']
            elif use_accelerated_alba is True:
                fragment_cache_info = ['alba', {'albamgr_cfg_url': Configuration.get_configuration_path(config_tree.format('abm_aa')),
                                                'bucket_strategy': ['1-to-1', {'prefix': vpool.guid,
                                                                               'preset': vpool.metadata['backend_aa_{0}'.format(storagerouter.guid)]['backend_info']['preset']}],
                                                'manifest_cache_size': 16 * 1024 * 1024 * 1024,
                                                'cache_on_read': fragment_cache_on_read,
                                                'cache_on_write': fragment_cache_on_write}]
            else:
                fragment_cache_info = ['local', {'path': sdp_frag.path,
                                                 'max_size': frag_size,
                                                 'cache_on_read': fragment_cache_on_read,
                                                 'cache_on_write': fragment_cache_on_write}]

            Configuration.set(config_tree.format('main'), json.dumps({
                'log_level': 'info',
                'port': alba_proxy.service.ports[0],
                'ips': [storagedriver.storage_ip],
                'manifest_cache_size': manifest_cache_size,
                'fragment_cache': fragment_cache_info,
                'transport': 'tcp',
                'albamgr_cfg_url': Configuration.get_configuration_path(config_tree.format('abm'))
            }, indent=4), raw=True)
            Configuration.set('/ovs/vpools/{0}/proxies/scrub/generic_scrub'.format(vpool.guid), json.dumps({
                'log_level': 'info',
                'port': 0,  # Will be overruled by the scrubber scheduled task
                'ips': ['127.0.0.1'],
                'manifest_cache_size': manifest_cache_size,
                'fragment_cache': ['none'],
                'transport': 'tcp',
                'albamgr_cfg_url': Configuration.get_configuration_path(config_tree.format('abm'))
            }, indent=4), raw=True)

        ###########################
        # CONFIGURE STORAGEDRIVER #
        ###########################
        storagedriver_config = StorageDriverConfiguration('storagedriver', vpool.guid, storagedriver.storagedriver_id)
        storagedriver_config.load()

        sco_size = sd_config_params['sco_size']
        dtl_mode = sd_config_params['dtl_mode']
        cluster_size = sd_config_params['cluster_size']
        dtl_transport = sd_config_params['dtl_transport']
        tlog_multiplier = StorageDriverClient.TLOG_MULTIPLIER_MAP[sco_size]
        sco_factor = float(write_buffer) / tlog_multiplier / sco_size  # sco_factor = write buffer / tlog multiplier (default 20) / sco size (in MiB)

        filesystem_config = {'fs_dtl_host': '',
                             'fs_enable_shm_interface': 0,
                             'fs_enable_network_interface': 1,
                             'fs_metadata_backend_arakoon_cluster_nodes': [],
                             'fs_metadata_backend_mds_nodes': [],
                             'fs_metadata_backend_type': 'MDS',
                             'fs_virtual_disk_format': 'raw',
                             'fs_raw_disk_suffix': '.raw'}
        if dtl_mode == 'no_sync':
            filesystem_config['fs_dtl_config_mode'] = StorageDriverClient.VOLDRV_DTL_MANUAL_MODE
        else:
            filesystem_config['fs_dtl_mode'] = StorageDriverClient.VPOOL_DTL_MODE_MAP[dtl_mode]
            filesystem_config['fs_dtl_config_mode'] = StorageDriverClient.VOLDRV_DTL_AUTOMATIC_MODE

        volume_manager_config = {'tlog_path': sdp_tlogs.path,
                                 'metadata_path': sdp_metadata.path,
                                 'clean_interval': 1,
                                 'dtl_throttle_usecs': 4000,
                                 'default_cluster_size': cluster_size * 1024,
                                 'number_of_scos_in_tlog': tlog_multiplier,
                                 'non_disposable_scos_factor': sco_factor}

        queue_urls = []
        mq_protocol = Configuration.get('/ovs/framework/messagequeue|protocol')
        mq_user = Configuration.get('/ovs/framework/messagequeue|user')
        mq_password = Configuration.get('/ovs/framework/messagequeue|password')
        for current_storagerouter in StorageRouterList.get_masters():
            queue_urls.append({'amqp_uri': '{0}://{1}:{2}@{3}'.format(mq_protocol, mq_user, mq_password, current_storagerouter.ip)})

        backend_connection_manager = {'alba_connection_host': storagedriver.storage_ip,
                                      'alba_connection_port': alba_proxy.service.ports[0],
                                      'alba_connection_preset': vpool.metadata['backend']['backend_info']['preset'],
                                      'alba_connection_timeout': 15,
                                      'alba_connection_transport': 'TCP',
                                      'backend_type': 'ALBA',
                                      'backend_interface_retries_on_error': 5,
                                      'backend_interface_retry_interval_secs': 1,
                                      'backend_interface_retry_backoff_multiplier': 2.0}
        has_rdma = Configuration.get('/ovs/framework/rdma')
        if use_accelerated_alba is False and has_rdma is True:
            backend_connection_manager['alba_connection_rora_manifest_cache_capacity'] = manifest_cache_size
            backend_connection_manager['alba_connection_use_rora'] = True

        volume_router = {'vrouter_id': vrouter_id,
                         'vrouter_redirect_timeout_ms': '5000',
                         'vrouter_routing_retries': 10,
                         'vrouter_volume_read_threshold': 1024,
                         'vrouter_volume_write_threshold': 1024,
                         'vrouter_file_read_threshold': 1024,
                         'vrouter_file_write_threshold': 1024,
                         'vrouter_min_workers': 4,
                         'vrouter_max_workers': 16,
                         'vrouter_sco_multiplier': sco_size * 1024 / cluster_size,  # sco multiplier = SCO size (in MiB) / cluster size (currently 4KiB),
                         'vrouter_backend_sync_timeout_ms': 5000,
                         'vrouter_migrate_timeout_ms': 5000,
                         'vrouter_use_fencing': True}

        storagedriver_config.configure_backend_connection_manager(**backend_connection_manager)
        storagedriver_config.configure_content_addressed_cache(serialize_read_cache=False,
                                                               read_cache_serialization_path=[])
        storagedriver_config.configure_scocache(scocache_mount_points=writecaches,
                                                trigger_gap='1GB',
                                                backoff_gap='2GB')
        storagedriver_config.configure_distributed_transaction_log(dtl_path=sdp_dtl.path,
                                                                   dtl_transport=StorageDriverClient.VPOOL_DTL_TRANSPORT_MAP[dtl_transport])
        storagedriver_config.configure_filesystem(**filesystem_config)
        storagedriver_config.configure_volume_manager(**volume_manager_config)
        storagedriver_config.configure_volume_router(**volume_router)
        storagedriver_config.configure_volume_router_cluster(vrouter_cluster_id=vpool.guid)
        storagedriver_config.configure_volume_registry(vregistry_arakoon_cluster_id=arakoon_cluster_name,
                                                       vregistry_arakoon_cluster_nodes=arakoon_nodes)
        storagedriver_config.configure_distributed_lock_store(dls_type='Arakoon',
                                                              dls_arakoon_cluster_id=arakoon_cluster_name,
                                                              dls_arakoon_cluster_nodes=arakoon_nodes)
        storagedriver_config.configure_file_driver(fd_cache_path=sdp_fd.path,
                                                   fd_extent_cache_capacity='1024',
                                                   fd_namespace='fd-{0}-{1}'.format(vpool_name, vpool.guid))
        storagedriver_config.configure_event_publisher(events_amqp_routing_key=Configuration.get('/ovs/framework/messagequeue|queues.storagedriver'),
                                                       events_amqp_uris=queue_urls)
        storagedriver_config.configure_threadpool_component(num_threads=16)
        storagedriver_config.save(client, reload_config=False)

        DiskController.sync_with_reality(storagerouter.guid)

        MDSServiceController.prepare_mds_service(storagerouter=storagerouter,
                                                 vpool=vpool,
                                                 fresh_only=True,
                                                 reload_config=False)

        ##################
        # START SERVICES #
        ##################
        sd_params = {'KILL_TIMEOUT': '30',
                     'VPOOL_NAME': vpool_name,
                     'VPOOL_MOUNTPOINT': storagedriver.mountpoint,
                     'CONFIG_PATH': storagedriver_config.remote_path,
                     'OVS_UID': client.run(['id', '-u', 'ovs']).strip(),
                     'OVS_GID': client.run(['id', '-g', 'ovs']).strip(),
                     'LOG_SINK': LogHandler.get_sink_path('storagedriver')}
        dtl_params = {'DTL_PATH': sdp_dtl.path,
                      'DTL_ADDRESS': storagedriver.storage_ip,
                      'DTL_PORT': str(storagedriver.ports['dtl']),
                      'DTL_TRANSPORT': 'RSocket' if has_rdma else 'TCP',
                      'LOG_SINK': LogHandler.get_sink_path('storagedriver')}
        alba_proxy_params = {'VPOOL_NAME': vpool_name,
                             'LOG_SINK': LogHandler.get_sink_path('alba_proxy'),
                             'CONFIG_PATH': Configuration.get_configuration_path('/ovs/vpools/{0}/proxies/{1}/config/main'.format(vpool.guid,
                                                                                                                                  storagedriver.alba_proxy_guid))}
        sd_service = 'ovs-volumedriver_{0}'.format(vpool.name)
        dtl_service = 'ovs-dtl_{0}'.format(vpool.name)
        alba_proxy_service = 'ovs-albaproxy_{0}'.format(vpool.name)

        ServiceManager.add_service(name='ovs-dtl', params=dtl_params, client=root_client, target_name=dtl_service)
        ServiceManager.start_service(dtl_service, client=root_client)
        ServiceManager.add_service(name='ovs-albaproxy', params=alba_proxy_params, client=root_client, target_name=alba_proxy_service)
        ServiceManager.start_service(alba_proxy_service, client=root_client)
        ServiceManager.add_service(name='ovs-volumedriver', params=sd_params, client=root_client, target_name=sd_service, additional_dependencies=[alba_proxy_service])

        storagedriver = StorageDriver(storagedriver.guid)
        current_startup_counter = storagedriver.startup_counter
        ServiceManager.start_service(sd_service, client=root_client)
        tries = 60
        while storagedriver.startup_counter == current_startup_counter and tries > 0:
            StorageRouterController._logger.debug('Waiting for the StorageDriver to start up...')
            if ServiceManager.get_service_status(sd_service, client=root_client)[0] is False:
                vpool.status = VPool.STATUSES.FAILURE
                vpool.save()
                raise RuntimeError('StorageDriver service failed to start (service not running)')
            tries -= 1
            time.sleep(60 - tries)
            storagedriver = StorageDriver(storagedriver.guid)
        if storagedriver.startup_counter == current_startup_counter:
            vpool.status = VPool.STATUSES.FAILURE
            vpool.save()
            raise RuntimeError('StorageDriver service failed to start (got no event)')
        StorageRouterController._logger.debug('StorageDriver running')

        ###############
        # POST CHECKS #
        ###############
        mds_config_set = MDSServiceController.get_mds_storagedriver_config_set(vpool=vpool, check_online=not offline_nodes_detected)
        for sr in all_storagerouters:
            if sr.ip not in ip_client_map:
                continue
            node_client = ip_client_map[sr.ip]['ovs']
            storagedriver_config = StorageDriverConfiguration('storagedriver', vpool.guid, storagedriver.storagedriver_id)
            storagedriver_config.load()
            if storagedriver_config.is_new is False:
                storagedriver_config.configure_filesystem(fs_metadata_backend_mds_nodes=mds_config_set[sr.guid])
                storagedriver_config.save(node_client)

        # Everything's reconfigured, refresh new cluster configuration
        sd_client = StorageDriverClient.load(vpool)
        for current_storagedriver in vpool.storagedrivers:
            if current_storagedriver.storagerouter.ip not in ip_client_map:
                continue
            sd_client.update_cluster_node_configs(str(current_storagedriver.storagedriver_id))

        # Fill vPool size
        with remote(root_client.ip, [os], 'root') as rem:
            vfs_info = rem.os.statvfs('/mnt/{0}'.format(vpool_name))
            vpool.size = vfs_info.f_blocks * vfs_info.f_bsize
            vpool.status = VPool.STATUSES.RUNNING
            vpool.save()

        vpool.invalidate_dynamics(['configuration'])
        if offline_nodes_detected is True:
            try:
                VDiskController.dtl_checkup(vpool_guid=vpool.guid, ensure_single_timeout=600)
            except:
                pass
            try:
                for vdisk in vpool.vdisks:
                    MDSServiceController.ensure_safety(vdisk=vdisk)
            except:
                pass
        else:
            VDiskController.dtl_checkup(vpool_guid=vpool.guid, ensure_single_timeout=600)
            for vdisk in vpool.vdisks:
                MDSServiceController.ensure_safety(vdisk=vdisk)
        StorageRouterController._logger.info('Add vPool {0} ended successfully'.format(vpool_name))

    @staticmethod
    @celery.task(name='ovs.storagerouter.remove_storagedriver')
    def remove_storagedriver(storagedriver_guid, offline_storage_router_guids=None):
        """
        Removes a Storage Driver (if its the last Storage Driver for a vPool, the vPool is removed as well)
        :param storagedriver_guid: Guid of the Storage Driver to remove
        :type storagedriver_guid: str
        :param offline_storage_router_guids: Guids of Storage Routers which are offline and will be removed from cluster.
                                             WHETHER VPOOL WILL BE DELETED DEPENDS ON THIS
        :type offline_storage_router_guids: list
        :return: None
        """
        storage_driver = StorageDriver(storagedriver_guid)
        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Deleting Storage Driver {1}'.format(storage_driver.guid, storage_driver.name))

        if offline_storage_router_guids is None:
            offline_storage_router_guids = []

        # Validations
        vpool = storage_driver.vpool
        if vpool.status != VPool.STATUSES.RUNNING:
            raise ValueError('VPool should be in {0} status'.format(VPool.STATUSES.RUNNING))

        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Checking availability of related Storage Routers'.format(storage_driver.guid, storage_driver.name))
        client = None
        temp_client = None
        errors_found = False
        storage_router = storage_driver.storagerouter
        storage_drivers_left = False
        storage_router_online = True
        storage_routers_offline = [StorageRouter(storage_router_guid) for storage_router_guid in offline_storage_router_guids]
        available_storage_drivers = []
        for sd in vpool.storagedrivers:
            sr = sd.storagerouter
            if sr != storage_router:
                storage_drivers_left = True
            try:
                temp_client = SSHClient(sr, username='root')
                if sr in storage_routers_offline:
                    raise Exception('Storage Router "{0}" passed as "offline Storage Router" appears to be reachable'.format(sr.name))
                with remote(temp_client.ip, [LocalStorageRouterClient]) as rem:
                    sd_key = '/ovs/vpools/{0}/hosts/{1}/config'.format(vpool.guid, sd.storagedriver_id)
                    if Configuration.exists(sd_key) is True:
                        path = Configuration.get_configuration_path(sd_key)
                        lsrc = rem.LocalStorageRouterClient(path)
                        lsrc.server_revision()  # 'Cheap' call to verify whether volumedriver is responsive
                        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Available Storage Driver for migration - {1}'.format(storage_driver.guid, sd.name))
                        available_storage_drivers.append(sd)
                client = temp_client
                StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Storage Router {1} with IP {2} is online'.format(storage_driver.guid, sr.name, sr.ip))
            except UnableToConnectException:
                if sr == storage_router or sr in storage_routers_offline:
                    StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Storage Router {1} with IP {2} is offline'.format(storage_driver.guid, sr.name, sr.ip))
                    if sr == storage_router:
                        storage_router_online = False
                else:
                    raise RuntimeError('Not all StorageRouters are reachable')
            except Exception as ex:
                if 'ClusterNotReachableException' in str(ex):
                    if sd != storage_driver:
                        raise RuntimeError('Not all StorageDrivers are reachable, please (re)start them and try again')
                    if client is None:
                        client = temp_client
                else:
                    raise

        if client is None:
            raise RuntimeError('Could not found any responsive node in the cluster')

        storage_driver.invalidate_dynamics('vdisks_guids')
        if len(storage_driver.vdisks_guids) > 0:
            raise RuntimeError('There are still vDisks served from the given Storage Driver')

        if storage_drivers_left and len(available_storage_drivers) == 0:
            raise RuntimeError('vPool is spread over several other Storage Drivers, but none of them are responsive')

        # Start removal
        if storage_drivers_left is True:
            vpool.status = VPool.STATUSES.SHRINKING
        else:
            vpool.status = VPool.STATUSES.DELETING
        vpool.save()

        available_sr_names = [sd.storagerouter.name for sd in available_storage_drivers]
        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Storage Routers on which an available Storage Driver runs: {1}'.format(storage_driver.guid, ', '.join(available_sr_names)))

        # Remove stale vDisks
        voldrv_vdisks = [entry.object_id() for entry in vpool.objectregistry_client.get_all_registrations()]
        voldrv_vdisk_guids = VDiskList.get_in_volume_ids(voldrv_vdisks).guids
        for vdisk_guid in set(vpool.vdisks_guids).difference(set(voldrv_vdisk_guids)):
            StorageRouterController._logger.warning('vDisk with guid {0} does no longer exist on any StorageDriver linked to vPool {1}, deleting...'.format(vdisk_guid, vpool.name))
            VDiskController.clean_vdisk_from_model(vdisk=VDisk(vdisk_guid))

        # Unconfigure or reconfigure the MDSes
        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Reconfiguring MDSes'.format(storage_driver.guid))
        vdisks = []
        mds_services_to_remove = [mds_service for mds_service in vpool.mds_services if mds_service.service.storagerouter_guid == storage_router.guid]
        for mds in mds_services_to_remove:
            for junction in mds.vdisks:
                vdisk = junction.vdisk
                if vdisk in vdisks:
                    continue
                vdisks.append(vdisk)
                vdisk.invalidate_dynamics(['info', 'storagedriver_id'])
                if vdisk.storagedriver_id:
                    try:
                        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Virtual Disk {1} {2} - Ensuring MDS safety'.format(storage_driver.guid, vdisk.guid, vdisk.name))
                        MDSServiceController.ensure_safety(vdisk=vdisk,
                                                           excluded_storagerouters=[storage_router] + storage_routers_offline)
                    except Exception:
                        StorageRouterController._logger.exception('Remove Storage Driver - Guid {0} - Virtual Disk {1} {2} - Ensuring MDS safety failed'.format(storage_driver.guid, vdisk.guid, vdisk.name))

        arakoon_cluster_name = str(Configuration.get('/ovs/framework/arakoon_clusters|voldrv'))
        config = ArakoonClusterConfig(cluster_id=arakoon_cluster_name, filesystem=False)
        config.load_config()
        arakoon_node_configs = []
        offline_node_ips = [sr.ip for sr in storage_routers_offline]
        for node in config.nodes:
            if node.ip in offline_node_ips or (node.ip == storage_router.ip and storage_router_online is False):
                continue
            arakoon_node_configs.append(ArakoonNodeConfig(str(node.name), str(node.ip), node.client_port))
        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Arakoon node configs - \n{1}'.format(storage_driver.guid, '\n'.join([str(config) for config in arakoon_node_configs])))
        vrouter_clusterregistry = ClusterRegistry(str(vpool.guid), arakoon_cluster_name, arakoon_node_configs)

        # Disable and stop DTL, voldrv and albaproxy services
        if storage_router_online is True:
            dtl_service = 'dtl_{0}'.format(vpool.name)
            voldrv_service = 'volumedriver_{0}'.format(vpool.name)
            albaproxy_service = 'albaproxy_{0}'.format(vpool.name)
            client = SSHClient(storage_router, username='root')

            for service in [voldrv_service, dtl_service]:
                try:
                    if ServiceManager.has_service(service, client=client):
                        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Stopping service {1}'.format(storage_driver.guid, service))
                        ServiceManager.stop_service(service, client=client)
                        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Removing service {1}'.format(storage_driver.guid, service))
                        ServiceManager.remove_service(service, client=client)
                except Exception:
                    StorageRouterController._logger.exception('Remove Storage Driver - Guid {0} - Disabling/stopping service {1} failed'.format(storage_driver.guid, service))
                    errors_found = True

            sd_config_key = '/ovs/vpools/{0}/hosts/{1}/config'.format(vpool.guid, storage_driver.storagedriver_id)
            if storage_drivers_left is False and Configuration.exists(sd_config_key):
                try:
                    if ServiceManager.has_service(albaproxy_service, client=client):
                        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Starting Alba proxy'.format(storage_driver.guid))
                        ServiceManager.start_service(albaproxy_service, client=client)
                        tries = 10
                        running = False
                        port = storage_driver.alba_proxy.service.ports[0]
                        while running is False and tries > 0:
                            StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Waiting for the Alba proxy to start up'.format(storage_driver.guid))
                            tries -= 1
                            time.sleep(10 - tries)
                            try:
                                client.run(['alba', 'proxy-statistics', '--host', storage_driver.storage_ip, '--port', str(port)])
                                running = True
                            except CalledProcessError as ex:
                                StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Fetching alba proxy-statistics failed with error (but ignoring): {1}'.format(storage_driver.guid, ex))
                        if running is False:
                            raise RuntimeError('Alba proxy failed to start')
                        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Alba proxy running'.format(storage_driver.guid))

                    StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Destroying filesystem and erasing node configs'.format(storage_driver.guid))
                    with remote(client.ip, [LocalStorageRouterClient], username='root') as rem:
                        path = Configuration.get_configuration_path(sd_config_key)
                        storagedriver_client = rem.LocalStorageRouterClient(path)
                        try:
                            storagedriver_client.destroy_filesystem()
                        except RuntimeError as rte:
                            # If backend has already been deleted, we cannot delete the filesystem anymore --> storage leak!!!
                            # @TODO: Find better way for catching this error
                            if 'MasterLookupResult.Error' not in rte.message:
                                raise

                    # noinspection PyArgumentList
                    vrouter_clusterregistry.erase_node_configs()
                except RuntimeError:
                    StorageRouterController._logger.exception('Remove Storage Driver - Guid {0} - Destroying filesystem and erasing node configs failed'.format(storage_driver.guid))
                    errors_found = True
            try:
                if ServiceManager.has_service(albaproxy_service, client=client):
                    StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Stopping service {1}'.format(storage_driver.guid, albaproxy_service))
                    ServiceManager.stop_service(albaproxy_service, client=client)
                    StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Removing service {1}'.format(storage_driver.guid, albaproxy_service))
                    ServiceManager.remove_service(albaproxy_service, client=client)
            except Exception:
                StorageRouterController._logger.exception('Remove Storage Driver - Guid {0} - Disabling/stopping service {1} failed'.format(storage_driver.guid, albaproxy_service))
                errors_found = True

        # Reconfigure volumedriver arakoon cluster
        try:
            if storage_drivers_left is True:
                StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Reconfiguring volumedriver arakoon cluster'.format(storage_driver.guid))
                node_configs = []
                for sd in available_storage_drivers:
                    if sd != storage_driver:
                        node_configs.append(ClusterNodeConfig(**sd.cluster_node_config))
                StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Node configs - \n{1}'.format(storage_driver.guid, '\n'.join([str(config) for config in node_configs])))
                vrouter_clusterregistry.set_node_configs(node_configs)
                srclient = StorageDriverClient.load(vpool)
                for sd in available_storage_drivers:
                    if sd != storage_driver:
                        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Storage Driver {1} {2} - Updating cluster node configs'.format(storage_driver.guid, sd.guid, sd.name))
                        srclient.update_cluster_node_configs(str(sd.storagedriver_id))
        except Exception:
            StorageRouterController._logger.exception('Remove Storage Driver - Guid {0} - Reconfiguring volumedriver arakoon cluster failed'.format(storage_driver.guid))
            errors_found = True

        # Removing MDS services
        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Removing MDS services'.format(storage_driver.guid))
        for mds_service in mds_services_to_remove:
            # All MDSServiceVDisk object should have been deleted above
            try:
                StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Remove MDS service (number {1}) for Storage Router with IP {2}'.format(storage_driver.guid, mds_service.number, storage_router.ip))
                MDSServiceController.remove_mds_service(mds_service=mds_service,
                                                        vpool=vpool,
                                                        reconfigure=False,
                                                        allow_offline=not storage_router_online)
            except Exception:
                StorageRouterController._logger.exception('Remove Storage Driver - Guid {0} - Removing MDS service failed'.format(storage_driver.guid))
                errors_found = True

        # Clean up directories and files
        dirs_to_remove = []
        for sd_partition in storage_driver.partitions:
            dirs_to_remove.append(sd_partition.path)
            sd_partition.delete()

        if storage_driver.alba_proxy is not None:
            config_tree = '/ovs/vpools/{0}/proxies/{1}'.format(vpool.guid, storage_driver.alba_proxy.guid)
            Configuration.delete(config_tree)

        if storage_router_online is True:
            # Cleanup directories/files
            StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Deleting vPool related directories and files'.format(storage_driver.guid))
            dirs_to_remove.append(storage_driver.mountpoint)

            try:
                mountpoints = StorageRouterController._get_mountpoints(client)
                for dir_name in dirs_to_remove:
                    if dir_name and client.dir_exists(dir_name) and dir_name not in mountpoints and dir_name != '/':
                        client.dir_delete(dir_name)
            except Exception:
                StorageRouterController._logger.exception('Remove Storage Driver - Guid {0} - Failed to retrieve mountpoint information or delete directories'.format(storage_driver.guid))
                StorageRouterController._logger.warning('Remove Storage Driver - Guid {0} - Following directories should be checked why deletion is prevented: {1}'.format(storage_driver.guid, ', '.join(dirs_to_remove)))
                errors_found = True

            StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Synchronizing disks with reality'.format(storage_driver.guid))
            try:
                DiskController.sync_with_reality(storage_router.guid)
            except Exception:
                StorageRouterController._logger.exception('Remove Storage Driver - Guid {0} - Synchronizing disks with reality failed'.format(storage_driver.guid))
                errors_found = True

        Configuration.delete('/ovs/vpools/{0}/hosts/{1}'.format(vpool.guid, storage_driver.storagedriver_id))

        # Model cleanup
        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Cleaning up model'.format(storage_driver.guid))
        if storage_driver.alba_proxy is not None:
            StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Removing alba proxy service from model'.format(storage_driver.guid))
            service = storage_driver.alba_proxy.service
            storage_driver.alba_proxy.delete()
            service.delete()

        sd_can_be_deleted = True
        if storage_drivers_left is False:
            for relation in ['mds_services', 'storagedrivers', 'vdisks']:
                expected_amount = 1 if relation == 'storagedrivers' else 0
                if len(getattr(vpool, relation)) > expected_amount:
                    sd_can_be_deleted = False
                    break
        else:
            if storage_router.guid in vpool.metadata:
                vpool.metadata.pop(storage_router.guid)
                vpool.save()
            StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Checking DTL for all virtual disks in vPool {1} with guid {2}'.format(storage_driver.guid, vpool.name, vpool.guid))
            try:
                VDiskController.dtl_checkup(vpool_guid=vpool.guid, ensure_single_timeout=600)
            except Exception:
                StorageRouterController._logger.exception('Remove Storage Driver - Guid {0} - DTL checkup failed for vPool {1} with guid {2}'.format(storage_driver.guid, vpool.name, vpool.guid))

        if sd_can_be_deleted is True:
            storage_driver.delete()
            if storage_drivers_left is False:
                StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Removing vPool from model'.format(storage_driver.guid))
                vpool.delete()
                Configuration.delete('/ovs/vpools/{0}'.format(vpool.guid))
        else:
            try:
                vpool.delete()  # Try to delete the vPool to invoke a proper stacktrace to see why it can't be deleted
            except Exception:
                errors_found = True
                StorageRouterController._logger.exception('Remove Storage Driver - Guid {0} - Cleaning up vpool from the model failed'.format(storage_driver.guid))

        StorageRouterController._logger.info('Remove Storage Driver - Guid {0} - Running MDS checkup'.format(storage_driver.guid))
        try:
            MDSServiceController.mds_checkup()
        except Exception:
            StorageRouterController._logger.exception('Remove Storage Driver - Guid {0} - MDS checkup failed'.format(storage_driver.guid))

        if errors_found is True:
            if storage_drivers_left is True:
                vpool.status = VPool.STATUSES.FAILURE
                vpool.save()
            raise RuntimeError('1 or more errors occurred while trying to remove the storage driver. Please check the logs for more information')
        if storage_drivers_left is True:
            vpool.status = VPool.STATUSES.RUNNING
            vpool.save()

    @staticmethod
    @celery.task(name='ovs.storagerouter.get_version_info')
    def get_version_info(storagerouter_guid):
        """
        Returns version information regarding a given StorageRouter
        :param storagerouter_guid: Storage Router guid to get version information for
        :type storagerouter_guid: str
        :return: Version information
        :rtype: dict
        """
        client = SSHClient(StorageRouter(storagerouter_guid))
        return {'storagerouter_guid': storagerouter_guid,
                'versions': PackageManager.get_versions(client)}

    @staticmethod
    @celery.task(name='ovs.storagerouter.get_support_info')
    def get_support_info(storagerouter_guid):
        """
        Returns support information regarding a given StorageRouter
        :param storagerouter_guid: Storage Router guid to get support information for
        :type storagerouter_guid: str
        :return: Support information
        :rtype: dict
        """
        return {'storagerouter_guid': storagerouter_guid,
                'nodeid': System.get_my_machine_id(),
                'clusterid': Configuration.get('/ovs/framework/cluster_id'),
                'enabled': Configuration.get('/ovs/framework/support|enabled'),
                'enablesupport': Configuration.get('ovs/framework/support|enablesupport')}

    @staticmethod
    @celery.task(name='ovs.storagerouter.get_support_metadata')
    def get_support_metadata():
        """
        Returns support metadata for a given storagerouter. This should be a routed task!
        """
        return SupportAgent().get_heartbeat_data()

    @staticmethod
    @celery.task(name='ovs.storagerouter.get_logfiles')
    def get_logfiles(local_storagerouter_guid):
        """
        Collects logs, moves them to a web-accessible location and returns log tgz's filename
        :param local_storagerouter_guid: Storage Router guid to retrieve log files on
        :type local_storagerouter_guid: str
        :return: Name of tgz containing the logs
        :rtype: str
        """
        this_storagerouter = System.get_my_storagerouter()
        this_client = SSHClient(this_storagerouter, username='root')
        logfile = this_client.run(['ovs', 'collect', 'logs']).strip()
        logfilename = logfile.split('/')[-1]

        storagerouter = StorageRouter(local_storagerouter_guid)
        webpath = '/opt/OpenvStorage/webapps/frontend/downloads'
        client = SSHClient(storagerouter, username='root')
        client.dir_create(webpath)
        client.file_upload('{0}/{1}'.format(webpath, logfilename), logfile)
        client.run(['chmod', '666', '{0}/{1}'.format(webpath, logfilename)])
        return logfilename

    @staticmethod
    @celery.task(name='ovs.storagerouter.configure_support')
    def configure_support(enable, enable_support):
        """
        Configures support on all StorageRouters
        :param enable: If True support agent will be enabled and started, else disabled and stopped
        :type enable: bool
        :param enable_support: If False openvpn will be stopped
        :type enable_support: bool
        :return: True
        :rtype: bool
        """
        clients = []
        try:
            for storagerouter in StorageRouterList.get_storagerouters():
                clients.append((SSHClient(storagerouter), SSHClient(storagerouter, username='root')))
        except UnableToConnectException:
            raise RuntimeError('Not all StorageRouters are reachable')
        Configuration.set('/ovs/framework/support|enabled', enable)
        Configuration.set('/ovs/framework/support|enablesupport', enable_support)
        for ovs_client, root_client in clients:
            if enable_support is False:
                root_client.run('service openvpn stop')
                root_client.file_delete('/etc/openvpn/ovs_*')
            if enable is True:
                if not ServiceManager.has_service(StorageRouterController.SUPPORT_AGENT, client=root_client):
                    ServiceManager.add_service(StorageRouterController.SUPPORT_AGENT, client=root_client)
                ServiceManager.restart_service(StorageRouterController.SUPPORT_AGENT, client=root_client)
            else:
                if ServiceManager.has_service(StorageRouterController.SUPPORT_AGENT, client=root_client):
                    ServiceManager.stop_service(StorageRouterController.SUPPORT_AGENT, client=root_client)
                    ServiceManager.remove_service(StorageRouterController.SUPPORT_AGENT, client=root_client)
        return True

    @staticmethod
    @celery.task(name='ovs.storagerouter.mountpoint_exists')
    def mountpoint_exists(name, storagerouter_guid):
        """
        Checks whether a given mountpoint for a vPool exists
        :param name: Name of the mountpoint to check
        :type name: str
        :param storagerouter_guid: Guid of the StorageRouter on which to check for mountpoint existence
        :type storagerouter_guid: str
        :return: True if mountpoint not in use else False
        :rtype: bool
        """
        client = SSHClient(StorageRouter(storagerouter_guid))
        return client.dir_exists(directory='/mnt/{0}'.format(name))

    @staticmethod
    @celery.task(name='ovs.storagerouter.get_update_status')
    def get_update_status(storagerouter_ip):
        """
        Checks for new updates
        :param storagerouter_ip: IP of the Storage Router to check for updates
        :type storagerouter_ip: str
        :return: Update status for specified storage router
        :rtype: dict
        """
        # Check plugin requirements
        root_client = SSHClient(storagerouter_ip,
                                username='root')
        required_plugin_params = {'name': (str, None),             # Name of a subpart of the plugin and is used for translation in html. Eg: alba:packages.SDM
                                  'version': (str, None),          # Available version to be installed
                                  'namespace': (str, None),        # Name of the plugin and is used for translation in html. Eg: ALBA:packages.sdm
                                  'services': (list, str),         # Services which the plugin depends upon and should be stopped during update
                                  'packages': (list, str),         # Packages which contain the plugin code and should be updated
                                  'downtime': (list, tuple),       # Information about crucial services which will go down during the update
                                  'prerequisites': (list, tuple)}  # Information about prerequisites which are unmet (eg running vms for storage driver update)
        package_map = {}
        plugin_functions = Toolbox.fetch_hooks('update', 'metadata')
        for function in plugin_functions:
            output = function(root_client)
            if not isinstance(output, dict):
                raise ValueError('Update cannot continue. Failed to retrieve correct plugin information ({0})'.format(function.func_name))

            for key, value in output.iteritems():
                for out in value:
                    Toolbox.verify_required_params(required_plugin_params, out)
                if key not in package_map:
                    package_map[key] = []
                package_map[key] += value

        # Update apt (only our ovs apt repo)
        PackageManager.update(client=root_client)

        # Compare installed and candidate versions
        return_value = {'upgrade_ongoing': os.path.exists('/etc/upgrade_ongoing')}
        for gui_name, package_information in package_map.iteritems():
            return_value[gui_name] = []
            for package_info in package_information:
                version = package_info['version']
                if version:
                    gui_down = 'watcher-framework' in package_info['services'] or 'nginx' in package_info['services']
                    info_added = False
                    for index, item in enumerate(return_value[gui_name]):
                        if item['name'] == package_info['name']:
                            return_value[gui_name][index]['downtime'].extend(package_info['downtime'])
                            info_added = True
                            if gui_down is True and return_value[gui_name][index]['gui_down'] is False:
                                return_value[gui_name][index]['gui_down'] = True
                    if info_added is False:  # Some plugins can have same package dependencies as core and we only want to show each package once in GUI (Eg: Arakoon for core and ALBA)
                        return_value[gui_name].append({'to': version,
                                                       'name': package_info['name'],
                                                       'gui_down': gui_down,
                                                       'downtime': package_info['downtime'],
                                                       'namespace': package_info['namespace'],
                                                       'prerequisites': package_info['prerequisites']})
        return return_value

    @staticmethod
    @add_hooks('update', 'metadata')
    def get_metadata_framework(client):
        """
        Retrieve packages and services on which the framework depends
        :param client: SSHClient on which to retrieve the metadata
        :type client: SSHClient
        :return: List of dictionaries which contain services to restart,
                                                    packages to update,
                                                    information about potential downtime
                                                    information about unmet prerequisites
        :rtype: list
        """
        this_sr = StorageRouterList.get_by_ip(client.ip)
        srs = StorageRouterList.get_storagerouters()
        downtime = []
        fwk_cluster_name = Configuration.get('/ovs/framework/arakoon_clusters|ovsdb')
        metadata = ArakoonInstaller.get_arakoon_metadata_by_cluster_name(cluster_name=fwk_cluster_name)
        if metadata is None:
            raise ValueError('Expected exactly 1 arakoon cluster of type {0}, found None'.format(ServiceType.ARAKOON_CLUSTER_TYPES.FWK))

        if metadata['internal'] is True:
            ovsdb_cluster = [ser.storagerouter_guid for sr in srs for ser in sr.services if ser.type.name == ServiceType.SERVICE_TYPES.ARAKOON and ser.name == 'arakoon-ovsdb']
            downtime = [('ovs', 'ovsdb', None)] if len(ovsdb_cluster) < 3 and this_sr.guid in ovsdb_cluster else []

        ovs_info = PackageManager.verify_update_required(packages=['openvstorage-core', 'openvstorage-webapps', 'openvstorage-cinder-plugin'],
                                                         services=['watcher-framework', 'memcached'],
                                                         client=client)
        arakoon_info = PackageManager.verify_update_required(packages=['arakoon'],
                                                             services=['arakoon-ovsdb'],
                                                             client=client)

        return {'framework': [{'name': 'ovs',
                               'version': ovs_info['version'],
                               'services': ovs_info['services'],
                               'packages': ovs_info['packages'],
                               'downtime': [],
                               'namespace': 'ovs',
                               'prerequisites': []},
                              {'name': 'arakoon',
                               'version': arakoon_info['version'],
                               'services': arakoon_info['services'],
                               'packages': arakoon_info['packages'],
                               'downtime': downtime,
                               'namespace': 'ovs',
                               'prerequisites': []}]}

    @staticmethod
    @add_hooks('update', 'metadata')
    def get_metadata_volumedriver(client):
        """
        Retrieve packages and services on which the volumedriver depends
        :param client: SSHClient on which to retrieve the metadata
        :type client: SSHClient
        :return: List of dictionaries which contain services to restart,
                                                    packages to update,
                                                    information about potential downtime
                                                    information about unmet prerequisites
        :rtype: list
        """
        srs = StorageRouterList.get_storagerouters()
        this_sr = StorageRouterList.get_by_ip(client.ip)
        downtime = []
        key = '/ovs/framework/arakoon_clusters|voldrv'
        if Configuration.exists(key):
            sd_cluster_name = Configuration.get(key)
            metadata = ArakoonInstaller.get_arakoon_metadata_by_cluster_name(cluster_name=sd_cluster_name)
            if metadata is None:
                raise ValueError('Expected exactly 1 arakoon cluster of type {0}, found None'.format(ServiceType.ARAKOON_CLUSTER_TYPES.SD))

            if metadata['internal'] is True:
                voldrv_cluster = [ser.storagerouter_guid for sr in srs for ser in sr.services if ser.type.name == ServiceType.SERVICE_TYPES.ARAKOON and ser.name == 'arakoon-voldrv']
                downtime = [('ovs', 'voldrv', None)] if len(voldrv_cluster) < 3 and this_sr.guid in voldrv_cluster else []

        alba_proxies = []
        alba_downtime = []
        for sr in srs:
            for service in sr.services:
                if service.type.name == ServiceType.SERVICE_TYPES.ALBA_PROXY and service.storagerouter_guid == this_sr.guid:
                    alba_proxies.append(service.alba_proxy)
                    alba_downtime.append(('ovs', 'proxy', service.alba_proxy.storagedriver.vpool.name))

        prerequisites = []
        volumedriver_services = ['ovs-volumedriver_{0}'.format(sd.vpool.name)
                                 for sd in this_sr.storagedrivers]
        volumedriver_services.extend(['ovs-dtl_{0}'.format(sd.vpool.name)
                                      for sd in this_sr.storagedrivers])
        voldrv_info = PackageManager.verify_update_required(packages=['volumedriver-base', 'volumedriver-server',
                                                                      'volumedriver-no-dedup-base', 'volumedriver-no-dedup-server'],
                                                            services=volumedriver_services,
                                                            client=client)
        alba_info = PackageManager.verify_update_required(packages=['alba'],
                                                          services=[service.service.name for service in alba_proxies],
                                                          client=client)
        arakoon_info = PackageManager.verify_update_required(packages=['arakoon'],
                                                             services=['arakoon-voldrv'],
                                                             client=client)

        return {'volumedriver': [{'name': 'volumedriver',
                                  'version': voldrv_info['version'],
                                  'services': voldrv_info['services'],
                                  'packages': voldrv_info['packages'],
                                  'downtime': alba_downtime,
                                  'namespace': 'ovs',
                                  'prerequisites': prerequisites},
                                 {'name': 'alba',
                                  'version': alba_info['version'],
                                  'services': alba_info['services'],
                                  'packages': alba_info['packages'],
                                  'downtime': alba_downtime,
                                  'namespace': 'ovs',
                                  'prerequisites': prerequisites},
                                 {'name': 'arakoon',
                                  'version': arakoon_info['version'],
                                  'services': arakoon_info['services'],
                                  'packages': arakoon_info['packages'],
                                  'downtime': downtime,
                                  'namespace': 'ovs',
                                  'prerequisites': []}]}

    @staticmethod
    @celery.task(name='ovs.storagerouter.update_framework')
    def update_framework(storagerouter_ip):
        """
        Launch the update_framework method in setup.py
        :param storagerouter_ip: IP of the Storage Router to update the framework packages on
        :type storagerouter_ip: str
        :return: None
        """
        root_client = SSHClient(storagerouter_ip,
                                username='root')
        root_client.run(['ovs', 'update', 'framework'])

    @staticmethod
    @celery.task(name='ovs.storagerouter.update_volumedriver')
    def update_volumedriver(storagerouter_ip):
        """
        Launch the update_volumedriver method in setup.py
        :param storagerouter_ip: IP of the Storage Router to update the volumedriver packages on
        :type storagerouter_ip: str
        :return: None
        """
        root_client = SSHClient(storagerouter_ip,
                                username='root')
        root_client.run(['ovs', 'update', 'volumedriver'])

    @staticmethod
    @celery.task(name='ovs.storagerouter.refresh_hardware')
    def refresh_hardware(storagerouter_guid):
        """
        Refreshes all hardware related information
        :param storagerouter_guid: Guid of the Storage Router to refresh the hardware on
        :type storagerouter_guid: str
        :return: None
        """
        StorageRouterController.set_rdma_capability(storagerouter_guid)
        DiskController.sync_with_reality(storagerouter_guid)

    @staticmethod
    def set_rdma_capability(storagerouter_guid):
        """
        Check if the Storage Router has been reconfigured to be able to support RDMA
        :param storagerouter_guid: Guid of the Storage Router to check and set
        :type storagerouter_guid: str
        :return: None
        """
        storagerouter = StorageRouter(storagerouter_guid)
        client = SSHClient(storagerouter, username='root')
        rdma_capable = False
        with remote(client.ip, [os], username='root') as rem:
            for root, dirs, files in rem.os.walk('/sys/class/infiniband'):
                for directory in dirs:
                    ports_dir = '/'.join([root, directory, 'ports'])
                    if not rem.os.path.exists(ports_dir):
                        continue
                    for sub_root, sub_dirs, _ in rem.os.walk(ports_dir):
                        if sub_root != ports_dir:
                            continue
                        for sub_directory in sub_dirs:
                            state_file = '/'.join([sub_root, sub_directory, 'state'])
                            if rem.os.path.exists(state_file):
                                if 'ACTIVE' in client.run(['cat', state_file]):
                                    rdma_capable = True
        storagerouter.rdma_capable = rdma_capable
        storagerouter.save()

    @staticmethod
    @celery.task(name='ovs.storagerouter.configure_disk')
    @ensure_single(task_name='ovs.storagerouter.configure_disk', mode='CHAINED', global_timeout=1800)
    def configure_disk(storagerouter_guid, disk_guid, partition_guid, offset, size, roles):
        """
        Configures a partition
        :param storagerouter_guid: Guid of the Storage Router to configure a disk on
        :type storagerouter_guid: str
        :param disk_guid: Guid of the disk to configure
        :type disk_guid: str
        :param partition_guid: Guid of the partition on the disk
        :type partition_guid: str
        :param offset: Offset for the partition
        :type offset: int
        :param size: Size of the partition
        :type size: int
        :param roles: Roles assigned to the partition
        :type roles: list
        :return: None
        """
        storagerouter = StorageRouter(storagerouter_guid)
        for role in roles:
            if role not in DiskPartition.ROLES or role == DiskPartition.ROLES.BACKEND:
                raise RuntimeError('Invalid role specified: {0}'.format(role))
        disk = Disk(disk_guid)
        if disk.storagerouter_guid != storagerouter_guid:
            raise RuntimeError('The given Disk is not on the given StorageRouter')
        if partition_guid is None:
            StorageRouterController._logger.debug('Creating new partition - Offset: {0} bytes - Size: {1} bytes - Roles: {2}'.format(offset, size, roles))
            with remote(storagerouter.ip, [DiskTools], username='root') as rem:
                if len(disk.aliases) == 0:
                    raise ValueError('Disk {0} does not have any aliases'.format(disk.name))
                rem.DiskTools.create_partition(disk_alias=disk.aliases[0],
                                               disk_size=disk.size,
                                               partition_start=offset,
                                               partition_size=size)
            DiskController.sync_with_reality(storagerouter_guid)
            disk = Disk(disk_guid)
            end_point = offset + size
            partition = None
            for part in disk.partitions:
                if offset < part.offset + part.size and end_point > part.offset:
                    partition = part
                    break

            if partition is None:
                raise RuntimeError('No new partition detected on disk {0} after having created 1'.format(disk.name))
            StorageRouterController._logger.debug('Partition created')
        else:
            StorageRouterController._logger.debug('Using existing partition')
            partition = DiskPartition(partition_guid)
            if partition.disk_guid != disk_guid:
                raise RuntimeError('The given DiskPartition is not on the given Disk')
            if partition.filesystem in ['swap', 'linux_raid_member', 'LVM2_member']:
                raise RuntimeError("It is not allowed to assign roles on partitions of type: ['swap', 'linux_raid_member', 'LVM2_member']")
        if partition.filesystem is None or partition_guid is None:
            StorageRouterController._logger.debug('Creating filesystem')
            if len(partition.aliases) == 0:
                raise ValueError('Partition with offset {0} does not have any aliases'.format(partition.offset))
            with remote(storagerouter.ip, [DiskTools], username='root') as rem:
                rem.DiskTools.make_fs(partition_alias=partition.aliases[0])
            DiskController.sync_with_reality(storagerouter_guid)
            partition = DiskPartition(partition.guid)
            if partition.filesystem not in ['ext4', 'xfs']:
                raise RuntimeError('Unexpected filesystem')
            StorageRouterController._logger.debug('Filesystem created')
        if partition.mountpoint is None:
            StorageRouterController._logger.debug('Configuring mountpoint')
            with remote(storagerouter.ip, [DiskTools], username='root') as rem:
                counter = 1
                mountpoint = None
                while True:
                    mountpoint = '/mnt/{0}{1}'.format('ssd' if disk.is_ssd else 'hdd', counter)
                    counter += 1
                    if not rem.DiskTools.mountpoint_exists(mountpoint):
                        break
                StorageRouterController._logger.debug('Found mountpoint: {0}'.format(mountpoint))
                rem.DiskTools.add_fstab(partition_aliases=partition.aliases,
                                        mountpoint=mountpoint,
                                        filesystem=partition.filesystem)
                rem.DiskTools.mount(mountpoint)
            DiskController.sync_with_reality(storagerouter_guid)
            partition = DiskPartition(partition.guid)
            if partition.mountpoint != mountpoint:
                raise RuntimeError('Unexpected mountpoint')
            StorageRouterController._logger.debug('Mountpoint configured')
        partition.roles = roles
        partition.save()
        StorageRouterController._logger.debug('Partition configured')

    @staticmethod
    def _get_free_ports(client, ports_in_use, number):
        """
        Gets `number` free ports that are not in use and not reserved
        """
        machine_id = System.get_my_machine_id(client)
        port_range = Configuration.get('/ovs/framework/hosts/{0}/ports|storagedriver'.format(machine_id))
        ports = System.get_free_ports(port_range, ports_in_use, number, client)

        return ports if number != 1 else ports[0]

    @staticmethod
    def _check_scrub_partition_present():
        """
        Checks whether at least 1 scrub partition is present on any Storage Router
        :return: boolean
        """
        for storage_router in StorageRouterList.get_storagerouters():
            for disk in storage_router.disks:
                for partition in disk.partitions:
                    if DiskPartition.ROLES.SCRUB in partition.roles:
                        return True
        return False

    @staticmethod
    def _get_mountpoints(client):
        """
        Retrieve the mountpoints
        :param client: SSHClient to retrieve the mountpoints on
        :return: List of mountpoints
        """
        mountpoints = []
        for mountpoint in client.run(['mount', '-v']).strip().splitlines():
            mp = mountpoint.split(' ')[2] if len(mountpoint.split(' ')) > 2 else None
            if mp and not mp.startswith('/dev') and not mp.startswith('/proc') and not mp.startswith('/sys') and not mp.startswith('/run') and not mp.startswith('/mnt/alba-asd') and mp != '/':
                mountpoints.append(mp)
        return mountpoints

    @staticmethod
    def _retrieve_alba_arakoon_config(backend_guid, ovs_client):
        """
        Retrieve the ALBA Arakoon configuration
        :param backend_guid: Guid of the ALBA backend
        :type backend_guid: str
        :param ovs_client: OVS client object
        :type ovs_client: OVSClient
        :return: Arakoon configuration information
        :rtype: dict
        """
        task_id = ovs_client.get('/alba/backends/{0}/get_config_metadata'.format(backend_guid))
        successful, arakoon_config = ovs_client.wait_for_task(task_id, timeout=300)
        if successful is False:
            raise RuntimeError('Could not load metadata from environment {0}'.format(ovs_client.ip))
        return arakoon_config
