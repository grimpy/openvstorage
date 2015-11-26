# Copyright 2014 iNuron NV
#
# Licensed under the Open vStorage Modified Apache License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.openvstorage.org/license
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
ScheduledTaskController module
"""

import time
import os
import traceback
from celery.result import ResultSet
from celery.schedules import crontab
from ovs.celery_run import celery
from ovs.dal.hybrids.diskpartition import DiskPartition
from ovs.dal.hybrids.vdisk import VDisk
from ovs.dal.lists.storagedriverlist import StorageDriverList
from ovs.dal.lists.vdisklist import VDiskList
from ovs.dal.lists.vmachinelist import VMachineList
from ovs.dal.lists.servicelist import ServiceList
from ovs.extensions.db.arakoon.ArakoonManagement import ArakoonManagementEx
from ovs.extensions.generic.configuration import Configuration
from ovs.extensions.generic.sshclient import SSHClient
from ovs.extensions.generic.sshclient import UnableToConnectException
from ovs.extensions.generic.system import System
from ovs.extensions.generic.remote import Remote
from ovs.lib.helpers.decorators import ensure_single
from ovs.lib.mdsservice import MDSServiceController
from ovs.lib.vdisk import VDiskController
from ovs.lib.vmachine import VMachineController
from ovs.log.logHandler import LogHandler
from volumedriver.storagerouter import storagerouterclient

logger = LogHandler.get('lib', name='scheduled tasks')
storagerouterclient.Logger.setupLogging(LogHandler.load_path('storagerouterclient'))
# noinspection PyArgumentList
storagerouterclient.Logger.enableLogging()


class ScheduledTaskController(object):
    """
    This controller contains all scheduled task code. These tasks can be
    executed at certain intervals and should be self-containing
    """

    @staticmethod
    @celery.task(name='ovs.scheduled.snapshotall', bind=True, schedule=crontab(minute='0', hour='2-22'))
    @ensure_single(['ovs.scheduled.snapshotall', 'ovs.scheduled.delete_snapshots'])
    def snapshot_all():
        """
        Snapshots all VMachines
        """
        logger.info('[SSA] Started')
        if len(Configuration.get('ovs.plugins.snapshot')) > 0:
            logger.info('[SSA] Build-in snapshotting aborted. Snapshot plugin registered')
            return

        success = []
        fail = []
        machines = VMachineList.get_customer_vmachines()
        for machine in machines:
            try:
                VMachineController.snapshot(machineguid=machine.guid,
                                            label='',
                                            is_consistent=False,
                                            is_automatic=True)
                success.append(machine.guid)
            except:
                fail.append(machine.guid)
        logger.info('[SSA] Snapshots have been taken for {0} vMachines, {1} failed.'.format(len(success), len(fail)))

    @staticmethod
    @celery.task(name='ovs.scheduled.delete_snapshots', bind=True, schedule=crontab(minute='0', hour='1'))
    @ensure_single(['ovs.scheduled.delete_snapshots'])
    def delete_snapshots():
        """
        Delete snapshots policy

        Implemented delete snapshot policy:
        < 1d | 1d bucket | 1 | best of bucket   | 1d
        < 1w | 1d bucket | 6 | oldest of bucket | 7d = 1w
        < 1m | 1w bucket | 3 | oldest of bucket | 4w = 1m
        > 1m | delete

        :param timestamp: Timestamp to determine whether snapshots should be kept or not, if none provided, current time will be used
        """
        logger.info('Delete snapshots started')
        if len(Configuration.get('ovs.plugins.snapshot')) > 0:
            logger.info('Delete snapshots aborted. Snapshot plugin registered')
            return

        mark = time.time() + (7 * 24 * 60 * 60)  # All snapshots are removed after 7 days
        for vmachine in VMachineList.get_customer_vmachines():
            if any(vd.info['object_type'] in ['BASE'] for vd in vmachine.vdisks):
                for snapshot in vmachine.snapshots:
                    if int(snapshot['timestamp']) < mark:
                        for diskguid, snapshotguid in snapshot['snapshots'].iteritems():
                            VDiskController.delete_snapshot(diskguid=diskguid,
                                                            snapshotid=snapshotguid)
        for vdisk in VDiskList.get_without_vmachine():
            if vdisk.info['object_type'] in ['BASE']:
                for snapshot in vdisk.snapshots:
                    if int(snapshot['timestamp']) < mark:
                        VDiskController.delete_snapshot(diskguid=vdisk.guid,
                                                        snapshotid=snapshot['guid'])
        logger.info('Delete snapshots finished')

    @staticmethod
    @celery.task(name='ovs.scheduled.gather_scrub_work', bind=True, schedule=crontab(minute='0', hour='3'))
    @ensure_single(['ovs.scheduled.gather_scrub_work'])
    def gather_scrub_work():
        logger.info('Divide scrubbing work among allowed Storage Routers')

        scrub_locations = {}
        for storage_driver in StorageDriverList.get_storagedrivers():
            for partition in storage_driver.partitions:
                if DiskPartition.ROLES.SCRUB == partition.role:
                    logger.info('Scrub partition found on Storage Router {0}: {1}'.format(storage_driver.name, partition.folder))
                    if storage_driver.storagerouter not in scrub_locations:
                        try:
                            _ = SSHClient(storage_driver.storagerouter.ip)
                            scrub_locations[storage_driver.storagerouter] = str(partition.path)
                        except UnableToConnectException:
                            logger.warning('StorageRouter {0} is not reachable'.format(storage_driver.storagerouter.ip))

        if len(scrub_locations) == 0:
            raise RuntimeError('No scrub locations found')

        vdisk_guids = set()
        for vmachine in VMachineList.get_customer_vmachines():
            for vdisk in vmachine.vdisks:
                if vdisk.info['object_type'] in ['BASE'] and len(vdisk.child_vdisks) == 0:
                    vdisk_guids.add(vdisk.guid)
        for vdisk in VDiskList.get_without_vmachine():
            if vdisk.info['object_type'] in ['BASE'] and len(vdisk.child_vdisks) == 0:
                vdisk_guids.add(vdisk.guid)

        logger.info('Found {0} virtual disks which need to be check for scrub work'.format(len(vdisk_guids)))
        local_machineid = System.get_my_machine_id()
        local_scrub_location = None
        local_vdisks_to_scrub = []
        result_set = ResultSet([])
        storage_router_list = []

        for index, scrub_info in enumerate(scrub_locations.items()):
            start_index = index * len(vdisk_guids) / len(scrub_locations)
            end_index = (index + 1) * len(vdisk_guids) / len(scrub_locations)
            storage_router = scrub_info[0]
            vdisk_guids_to_scrub = list(vdisk_guids)[start_index:end_index]
            local = storage_router.machine_id == local_machineid
            logger.info('Executing scrub work on {0} Storage Router {1} for {2} virtual disks'.format('local' if local is True else 'remote', storage_router.name, len(vdisk_guids_to_scrub)))

            if local is True:
                local_scrub_location = scrub_info[1]
                local_vdisks_to_scrub = vdisk_guids_to_scrub
            else:
                result_set.add(ScheduledTaskController._execute_scrub_work.s(scrub_location=scrub_info[1],
                                                                             vdisk_guids=vdisk_guids_to_scrub).apply_async(
                    routing_key='sr.{0}'.format(storage_router.machine_id)
                ))
                storage_router_list.append(storage_router)
                logger.info('Launched scrub task on Storage Router {0}'.format(storage_router.name))

        # Remote tasks have been launched, now start the local task and then wait for remote tasks to finish
        if local_scrub_location is not None and len(local_vdisks_to_scrub) > 0:
            ScheduledTaskController._execute_scrub_work(scrub_location=local_scrub_location,
                                                        vdisk_guids=local_vdisks_to_scrub)
        all_results = result_set.join(propagate=False)  # Propagate False makes sure all jobs are waited for even when 1 or more jobs fail
        for index, result in enumerate(all_results):
            if result is not None:
                logger.error('Scrubbing failed on Storage Router {0} with error {1}'.format(storage_router_list[index].name, result))

    @staticmethod
    @celery.task(name='ovs.scheduled.execute_scrub_work')
    def _execute_scrub_work(scrub_location, vdisk_guids):
        def verify_mds_config(current_vdisk):
            current_vdisk.invalidate_dynamics(['info'])
            vdisk_configs = current_vdisk.info['metadata_backend_config']
            if len(vdisk_configs) == 0:
                raise RuntimeError('Could not load MDS configuration')
            return vdisk_configs

        logger.info('Scrub location: {0}'.format(scrub_location))
        total = len(vdisk_guids)
        skipped = 0
        storagedrivers = {}
        failures = []
        for vdisk_guid in vdisk_guids:
            vdisk = VDisk(vdisk_guid)
            try:
                # Load the vDisk's StorageDriver
                logger.info('Scrubbing virtual disk {0} with guid {1}'.format(vdisk.name, vdisk.guid))
                vdisk.invalidate_dynamics(['storagedriver_id'])
                if vdisk.storagedriver_id not in storagedrivers:
                    storagedrivers[vdisk.storagedriver_id] = StorageDriverList.get_by_storagedriver_id(vdisk.storagedriver_id)
                storagedriver = storagedrivers[vdisk.storagedriver_id]

                # Load the vDisk's MDS configuration
                configs = verify_mds_config(current_vdisk=vdisk)

                # Check MDS master is local. Trigger MDS handover if necessary
                if configs[0].get('ip') != storagedriver.storagerouter.ip:
                    logger.debug('MDS for volume {0} is not local. Trigger handover'.format(vdisk.volume_id))
                    MDSServiceController.ensure_safety(vdisk)
                    configs = verify_mds_config(current_vdisk=vdisk)
                    if configs[0].get('ip') != storagedriver.storagerouter.ip:
                        skipped += 1
                        logger.info('Skipping scrubbing work unit for volume {0}: MDS master is not local'.format(vdisk.volume_id))
                        continue
                with vdisk.storagedriver_client.make_locked_client(str(vdisk.volume_id)) as locked_client:
                    work_units = locked_client.get_scrubbing_workunits()
                    for work_unit in work_units:
                        scrubbing_result = locked_client.scrub(work_unit, scrub_location)
                        locked_client.apply_scrubbing_result(scrubbing_result)
                    if work_units:
                        logger.info('Scrubbing successfully applied')
            except Exception, ex:
                failures.append('Failed scrubbing work unit for volume {0} with guid {1}: {2}'.format(vdisk.name, vdisk.guid, ex))

        failed = len(failures)
        logger.info('Scrubbing finished. {0} volumes: {1} success, {2} failed, {3} skipped.'.format(total, (total - failed - skipped), failed, skipped))
        if failed > 0:
            raise Exception("\n\n".join(failures))

    @staticmethod
    @celery.task(name='ovs.scheduled.collapse_arakoon', bind=True, schedule=crontab(minute='30', hour='0'))
    @ensure_single(['ovs.scheduled.collapse_arakoon'])
    def collapse_arakoon():
        logger.info('Starting arakoon collapse')
        arakoon_clusters = {}
        for service in ServiceList.get_services():
            if service.type.name in ('Arakoon', 'NamespaceManager', 'AlbaManager'):
                arakoon_clusters.setdefault(service.name.replace('arakoon-', ''), []).append(service.storagerouter.ip)

        for cluster, ips in arakoon_clusters.iteritems():
            if len(ips) > 0:
                ip = ips[0]
                logger.info('  Collapsing cluster: {0} using ip {1}'.format(cluster, ip))
                with Remote(ip, [ArakoonManagementEx])  as remote:
                    cluster_instance = remote.ArakoonManagementEx().getCluster(cluster)
                    for node in cluster_instance.listNodes():
                        logger.info('    Collapsing node: {0}'.format(node))
                        try:
                            cluster_instance.remoteCollapse(node, 2)  # Keep 2 tlogs
                        except Exception as e:
                            logger.exception('Error during collapsing cluster {0} node {1}'.format(cluster, node))
            else:
                logger.warning('Could not collapse cluster {0}. No IP addresses found'.format(cluster))

        logger.info('Arakoon collapse finished')
