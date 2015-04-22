# Copyright 2014 CloudFounders NV
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This module contains OpenStack Cinder commands
"""
import os
import time
import datetime
import subprocess
from ovs.extensions.generic.sshclient import SSHClient

CINDER_CONF = '/etc/cinder/cinder.conf'
CINDER_OPENSTACK_SERVICE = '/etc/init/cinder-volume.conf'
EXPORT = 'env PYTHONPATH="${PYTHONPATH}:/opt/OpenvStorage:/opt/OpenvStorage/webapps"'
EXPORT_ = 'env PYTHONPATH="\\\${PYTHONPATH}:/opt/OpenvStorage:/opt/OpenvStorage/webapps"'


class OpenStackCinder(object):
    """
    Represent the Cinder service
    """

    def __init__(self, cinder_password=None, cinder_user='admin', tenant_name='admin', controller_ip='127.0.0.1', sshclient_ip='127.0.0.1'):
        self.client = SSHClient(sshclient_ip)
        auth_url = 'http://{0}:35357/v2.0'.format(controller_ip)
        self.cinder_client = None

        if cinder_password is not None:
            try:
                from cinderclient.v1 import client as cinder_client
                self.cinder_client = cinder_client.Client(cinder_user, cinder_password, tenant_name, auth_url)
            except ImportError:
                pass

        self.is_openstack = os.path.exists(CINDER_OPENSTACK_SERVICE)
        self.is_devstack = False
        try:
            self.is_devstack = 'stack' in str(self.client.run('ps aux | grep SCREEN | grep stack | grep -v grep'))
        except subprocess.CalledProcessError:
            pass

    @property
    def is_cinder_running(self):
        return self._is_cinder_running()

    @property
    def is_cinder_installed(self):
        return self._is_cinder_installed()

    @staticmethod
    def valid_credentials(cinder_password, cinder_user, tenant_name, controller_ip):
        """
        Validate credentials
        """
        try:
            from cinderclient.v1 import client as cinder_client
        except ImportError:
            return False
        else:
            try:
                auth_url = 'http://{}:35357/v2.0'.format(controller_ip)
                cinder_client = cinder_client.Client(cinder_user, cinder_password, tenant_name, auth_url)
                cinder_client.authenticate()
                return True
            except:
                return False

    @staticmethod
    def _get_version():
        """
        Get openstack cinder version
        """
        try:
            from cinder import version
            version = version.version_string()
            if version.startswith('2015.2'):
                return 'kilo'  # For the moment use K driver
            elif version.startswith('2015.1'):
                return 'kilo'
            elif version.startswith('2014.2'):
                return 'juno'
            else:
                raise ValueError('Unknown cinder version: %s' % version)
        except Exception as ex:
            raise ValueError('Cannot determine cinder version: %s' % str(ex))

    @staticmethod
    def _get_existing_driver_version():
        """
        Get VERSION string from existing driver
        """
        try:
            from cinder.volume.drivers import openvstorage
            if hasattr(openvstorage, 'OVSVolumeDriver'):
                return getattr(openvstorage.OVSVolumeDriver, 'VERSION', '0.0.0')
        except ImportError:
            pass
        return '0.0.0'

    def _get_driver_code(self):
        """
        WGET driver, compare versions, allow local code to be updated from OVS repo until driver is patched upstream
        """
        version = self._get_version()
        remote_driver = "https://bitbucket.org/openvstorage/openvstorage/raw/default/openstack/cinder-volume-driver/%s/openvstorage.py" % version
        temp_location = "/tmp/openvstorage.py"
        self.client.run('wget {0} -P /tmp'.format(remote_driver))
        file_content = self.client.file_read(temp_location)
        remote_version = '0.0.0'
        for line in file_content:
            if 'VERSION = ' in line:
                remote_version = line.split('VERSION = ')[-1].strip().replace("'", "").replace('"', "")
                break

        existing_version = self._get_existing_driver_version()
        local_driver = None
        if self.is_devstack:
            cinder_base_path = self._get_base_path('cinder')
            local_driver = '{0}/volume/drivers/openvstorage.py'.format(cinder_base_path)
        elif self.is_openstack:
            local_driver = '/usr/lib/python2.7/dist-packages/cinder/volume/drivers/openvstorage.py'
        if remote_version > existing_version:
            print('Updating existing driver using {0} from version {1} to version {2}'.format(remote_driver, existing_version, remote_version))
            if self.is_devstack:
                self.client.run('cp {0} /opt/stack/cinder/cinder/volume/drivers'.format(temp_location))
            elif self.is_openstack:
                self.client.run('cp {0} /usr/lib/python2.7/dist-packages/cinder/volume/drivers'.format(temp_location))
        else:
            print('Using driver {0} version {1}'.format(local_driver, existing_version))
        self.client.run('rm {0}'.format(temp_location))

    def _is_cinder_running(self):
        if self.is_devstack:
            try:
                return 'cinder-volume' in str(self.client.run('ps aux | grep cinder-volume | grep -v grep'))
            except SystemExit:
                return False
        if self.is_openstack:
            try:
                return 'start/running' in str(self.client.run('service cinder-volume status'))
            except SystemExit:
                return False
        return False

    def _is_cinder_installed(self):
        return self.client.file_exists(CINDER_CONF)

    def configure_vpool(self, vpool_name):
        if self.is_devstack or self.is_openstack:
            self._get_driver_code()
            self._chown_mountpoint()
            self._configure_cinder_driver(vpool_name)
            self._create_volume_type(vpool_name)
            self._patch_etc_init_cindervolume_conf()
            self._apply_patches()
            self._restart_processes()

    def unconfigure_vpool(self, vpool_name, mountpoint, remove_volume_type):
        if self.is_devstack or self.is_openstack:
            self._unchown_mountpoint(mountpoint)
            self._unconfigure_cinder_driver(vpool_name)
            if remove_volume_type:
                self._delete_volume_type(vpool_name)
            self._unpatch_etc_init_cindervolume_conf()
            self._restart_processes()

    def _chown_mountpoint(self):
        # Vpool owned by stack / cinder
        # Give access to libvirt-qemu and ovs
        if self.is_devstack:
            self.client.run('usermod -a -G ovs libvirt-qemu')
            self.client.run('usermod -a -G ovs stack')
        elif self.is_openstack:
            self.client.run('usermod -a -G ovs libvirt-qemu')
            self.client.run('usermod -a -G ovs cinder')

    def _unchown_mountpoint(self, mountpoint):
        # self.client.run('chown root "{0}"'.format(mountpoint))
        pass

    def _configure_cinder_driver(self, vpool_name):
        """
        Adds a new cinder driver, multiple backends
        """
        if not self.client.file_exists(CINDER_CONF):
            return False

        self.client.run("""python -c '''from ConfigParser import RawConfigParser
changed = False
vpool_name = "%s"
CINDER_CONF = "%s"
cfg = RawConfigParser()
cfg.read([CINDER_CONF]);
if not cfg.has_section(vpool_name):
    changed = True
    cfg.add_section(vpool_name)
    cfg.set(vpool_name, "volume_driver", "cinder.volume.drivers.openvstorage.OVSVolumeDriver")
    cfg.set(vpool_name, "volume_backend_name", vpool_name)
    cfg.set(vpool_name, "vpool_name", vpool_name)
enabled_backends = []
if cfg.has_option("DEFAULT", "enabled_backends"):
    enabled_backends = cfg.get("DEFAULT", "enabled_backends").split(", ")
if not vpool_name in enabled_backends:
    changed = True
    enabled_backends.append(vpool_name)
    cfg.set("DEFAULT", "enabled_backends", ", ".join(enabled_backends))
    if changed:
        with open(CINDER_CONF, "w") as fp:
           cfg.write(fp)
'''""" % (vpool_name, CINDER_CONF))

    def _unconfigure_cinder_driver(self, vpool_name):
        """
        Removes a cinder driver, multiple backends
        """
        if not self.client.file_exists(CINDER_CONF):
            return False

        self.client.run("""python -c '''from ConfigParser import RawConfigParser
changed = False
vpool_name = "%s"
CINDER_CONF = "%s"
cfg = RawConfigParser()
cfg.read([CINDER_CONF]);
if cfg.has_section(vpool_name):
    changed = True
    cfg.remove_section(vpool_name)
enabled_backends = []
if cfg.has_option("DEFAULT", "enabled_backends"):
    enabled_backends = cfg.get("DEFAULT", "enabled_backends").split(", ")
if vpool_name in enabled_backends:
    changed = True
    enabled_backends.remove(vpool_name)
    cfg.set("DEFAULT", "enabled_backends", ", ".join(enabled_backends))
    if changed:
        with open(CINDER_CONF, "w") as fp:
           cfg.write(fp)
'''""" % (vpool_name, CINDER_CONF))

    def _restart_processes(self):
        """
        Restart the cinder process that uses the OVS volume driver
        - also restarts nova api and compute services
        """
        if self.is_devstack:
            self._restart_devstack_screen()
        else:
            self._restart_openstack_services()

    @staticmethod
    def _get_devstack_log_name(service, logdir='/opt/stack/logs'):
        """
        Construct a log name in format /opt/stack/logs/h-api-cw.log.2015-04-01-123300
        """
        now_time = datetime.datetime.strftime(datetime.datetime.now(), '%Y-%m-%d-%H%M%S')
        return '{0}/{1}.log.{2}'.format(logdir, service, now_time)

    def _restart_devstack_screen(self):
        """
        Restart c-vol on devstack
        resume logging
         screen -S stack -p h-api-cw -X logfile LOGFILE
         screen -S stack -p h-api-cw -X log on
         ln -sf /opt/stack/logs/h-api-cfn.log.2015-04-01-123300 /opt/stack/logs/screen-logs/screen-h-api-cfn.log
        """
        try:
            self.client.run('''su stack -c 'screen -S stack -p c-vol -X kill' ''')
            time.sleep(3)
            out = self.client.run('''su stack -c 'screen -S stack -p n-api -Q select 1>/dev/null; echo $?' ''')
            n_api_screen_exists = out == '0'
            if n_api_screen_exists:
                self.client.run('''su stack -c 'screen -S stack -p n-api -X stuff \n' ''')
                self.client.run('''su stack -c 'screen -S stack -p n-api -X kill' ''')
            time.sleep(3)
            self.client.run('''su stack -c 'screen -S stack -p n-cpu -X kill' ''')

            self.client.run('''su stack -c 'mkdir -p /opt/stack/logs' ''')
            time.sleep(3)
            logfile = self._get_devstack_log_name('c-vol')
            self.client.run('''su stack -c 'touch %s' ''' % logfile)
            self.client.run('''su stack -c 'screen -S stack -X screen -t c-vol' ''')
            self.client.run('''su stack -c 'screen -S stack -p c-vol -X logfile %s' ''' % logfile)
            self.client.run('''su stack -c 'screen -S stack -p c-vol -X log on' ''')
            self.client.run('rm /opt/stack/logs/c-vol.log || true')
            self.client.run('ln -sf %s /opt/stack/logs/c-vol.log' % logfile)

            self.client.run('''su stack -c 'screen -S stack -p c-vol -X stuff "export PYTHONPATH=\"${PYTHONPATH}:/opt/OpenvStorage\"\012"' ''')
            self.client.run('''su stack -c 'screen -S stack -p c-vol -X stuff "newgrp ovs\012"' ''')
            self.client.run('''su stack -c 'screen -S stack -p c-vol -X stuff "newgrp stack\012"' ''')
            self.client.run('''su stack -c 'screen -S stack -p c-vol -X stuff "umask 0002\012"' ''')
            self.client.run('''su stack -c 'screen -S stack -p c-vol -X stuff "/usr/local/bin/cinder-volume --config-file /etc/cinder/cinder.conf & echo \$! >/opt/stack/status/stack/c-vol.pid; fg || echo  c-vol failed to start | tee \"/opt/stack/status/stack/c-vol.failure\"\012"' ''')

            time.sleep(3)
            logfile = self._get_devstack_log_name('n-cpu')
            self.client.run('''su stack -c 'touch %s' ''' % logfile)
            self.client.run('''su stack -c 'screen -S stack -X screen -t n-cpu' ''')
            self.client.run('''su stack -c 'screen -S stack -p n-cpu -X logfile %s' ''' % logfile)
            self.client.run('''su stack -c 'screen -S stack -p n-cpu -X log on' ''')
            self.client.run('rm /opt/stack/logs/n-cpu.log || true')
            self.client.run('ln -sf %s /opt/stack/logs/n-cpu.log' % logfile)
            self.client.run('''su stack -c 'screen -S stack -p n-cpu -X stuff "newgrp ovs\012"' ''')
            self.client.run('''su stack -c 'screen -S stack -p n-cpu -X stuff "newgrp stack\012"' ''')
            self.client.run('''su stack -c 'screen -S stack -p n-cpu -X stuff "sg libvirtd /usr/local/bin/nova-compute --config-file /etc/nova/nova.conf & echo $! >/opt/stack/status/stack/n-cpu.pid; fg || echo n-cpu failed to start | tee \"/opt/stack/status/stack/n-cpu.failure\"\012"' ''')

            if n_api_screen_exists:
                time.sleep(3)
                logfile = self._get_devstack_log_name('n-api')
                self.client.run('''su stack -c 'touch %s' ''' % logfile)
                self.client.run('''su stack -c 'screen -S stack -X screen -t n-api' ''')
                self.client.run('''su stack -c 'screen -S stack -p n-api -X logfile %s' ''' % logfile)
                self.client.run('''su stack -c 'screen -S stack -p n-api -X log on' ''')
                self.client.run('rm /opt/stack/logs/n-api.log || true')
                self.client.run('ln -sf %s /opt/stack/logs/n-api.log' % logfile)
                self.client.run('''su stack -c 'screen -S stack -p n-api -X stuff "export PYTHONPATH=\"${PYTHONPATH}:/opt/OpenvStorage\"\012"' ''')
                self.client.run('''su stack -c 'screen -S stack -p n-api -X stuff "/usr/local/bin/nova-api & echo $! >/opt/stack/status/stack/n-api.pid; fg || echo n-api failed to start | tee \"/opt/stack/status/stack/n-api.failure\"\012"' ''')
        except SystemExit as se:  # failed command or non-zero exit codes raise SystemExit
            raise RuntimeError(str(se))
        return self._is_cinder_running()

    def _patch_etc_init_cindervolume_conf(self):
        """
        export PYTHONPATH in the upstart service conf file
        """
        if self.is_openstack and os.path.exists(CINDER_OPENSTACK_SERVICE):
            with open(CINDER_OPENSTACK_SERVICE, 'r') as cinder_file:
                contents = cinder_file.read()
            if EXPORT in contents:
                return True
            contents = contents.replace('\nexec start-stop-daemon', '\n\n{}\nexec start-stop-daemon'.format(EXPORT_))
            print('changing contents of cinder-volume service conf... %s' % (EXPORT_ in contents))
            self.client.run('cat >%s <<EOF \n%s' % (CINDER_OPENSTACK_SERVICE, contents))

    def _unpatch_etc_init_cindervolume_conf(self):
        """
        remove export PYTHONPATH from the upstart service conf file
        """
        if self.is_openstack and os.path.exists(CINDER_OPENSTACK_SERVICE):
            with open(CINDER_OPENSTACK_SERVICE, 'r') as cinder_file:
                contents = cinder_file.read()
            if EXPORT not in contents:
                return True
            contents = contents.replace(EXPORT_, '')
            print('fixed contents of cinder-volume service conf... %s' % (EXPORT_ in contents))
            self.client.run('cat >%s <<EOF \n%s' % (CINDER_OPENSTACK_SERVICE, contents))

    def _restart_openstack_services(self):
        """
        Restart services on openstack
        """
        for service_name in ('nova-compute', 'nova-api-os-compute', 'cinder-volume'):
            if os.path.exists('/etc/init/{0}.conf'.format(service_name)):
                try:
                    self.client.run('service {0} restart'.format(service_name))
                except SystemExit as sex:
                    print('Failed to restart service {0}. {1}'.format(service_name, sex))
        time.sleep(3)
        return self._is_cinder_running()

    def _create_volume_type(self, volume_type_name):
        """
        Create a cinder volume type, based on vpool name
        """
        if self.cinder_client is not None:
            volume_types = self.cinder_client.volume_types.list()
            for v in volume_types:
                if v.name == volume_type_name:
                    return False
            volume_type = self.cinder_client.volume_types.create(volume_type_name)
            volume_type.set_keys(metadata={'volume_backend_name': volume_type_name})

    def _apply_patches(self):
        nova_base_path = self._get_base_path('nova')

        version = self._get_version()
        # fix "blockdev" issue
        # fix "instance rename" issue
        # NOTE! vmachine.name and instance.display_name must match from the beginning
        nova_volume_file = None
        nova_driver_file = None
        nova_servers_file = None
        if self.is_devstack:
            nova_volume_file = '{0}/virt/libvirt/volume.py'.format(nova_base_path)
            nova_driver_file = '{0}/virt/libvirt/driver.py'.format(nova_base_path)
            nova_servers_file = '{0}/api/openstack/compute/servers.py'.format(nova_base_path)
        elif self.is_openstack:
            nova_volume_file = '/usr/lib/python2.7/dist-packages/nova/virt/libvirt/volume.py'
            nova_driver_file = '/usr/lib/python2.7/dist-packages/nova/virt/libvirt/driver.py'
            nova_servers_file = '/usr/lib/python2.7/dist-packages/nova/api/openstack/compute/servers.py'

        self.client.run("""python -c "
import os
version = '%s'
nova_volume_file = '%s'
nova_driver_file = '%s'
nova_servers_file = '%s'
with open(nova_volume_file, 'r') as f:
    file_contents = f.readlines()
new_class = '''
class LibvirtFileVolumeDriver(LibvirtBaseVolumeDriver):
    def __init__(self, connection):
        super(LibvirtFileVolumeDriver,
              self).__init__(connection, is_block_dev=False)

    def get_config(self, connection_info, disk_info):
        conf = super(LibvirtFileVolumeDriver,
                     self).get_config(connection_info, disk_info)
        conf.source_type = 'file'
        conf.source_path = connection_info['data']['device_path']
        return conf
'''
updatename = '''
        try:
            from ovs.lib.vmachine import VMachineController
            VMachineController.update_vmachine_name.apply_async(kwargs={'old_name':instance.display_name, 'new_name':body['server']['name']})
        except Exception as ex:
            print('[OVS] Update exception {0}'.format(ex))
'''
patched = False
for line in file_contents:
    if 'class LibvirtFileVolumeDriver(LibvirtBaseVolumeDriver):' in line:
        patched = True
        break

if not patched:
    fc = None
    for line in file_contents[:]:
        if line.startswith('class LibvirtVolumeDriver(LibvirtBaseVolumeDriver):'):
            fc = file_contents[:file_contents.index(line)] + [l+'\\n' for l in new_class.split('\\n')] + file_contents[file_contents.index(line):]
            break
    if fc is not None:
        with open(nova_volume_file, 'w') as f:
            f.writelines(fc)
with open(nova_driver_file, 'r') as f:
    file_contents = f.readlines()
patched = False
for line in file_contents:
    if 'file=nova.virt.libvirt.volume.LibvirtFileVolumeDriver' in line:
        patched = True
        break
if not patched:
    fc = None
    for line in file_contents[:]:
        if 'local=nova.virt.libvirt.volume.LibvirtVolumeDriver' in line:
            fc = file_contents[:file_contents.index(line)] + ['''                  'file=nova.virt.libvirt.volume.LibvirtFileVolumeDriver',\\n'''] + file_contents[file_contents.index(line):]
            break
    if fc is not None:
        with open(nova_driver_file, 'w') as f:
            f.writelines(fc)
if os.path.exists(nova_servers_file):
    fc = None
    if version == 'kilo':
        with open(nova_servers_file, 'r') as f:
            file_contents = f.readlines()
        patched = False
        for line in file_contents:
            if 'from ovs.lib.vmachine import VMachineController' in line:
                patched = True
                break
        defupdate = False
        if not patched:
            for line in file_contents[:]:
                if 'def update(self,' in line:
                    defupdate = True
                    continue
                if defupdate:
                    if 'instance = self._get_server(ctxt,' in line:
                       fc = file_contents[:file_contents.index(line)+1] + [l+'\\n' for l in updatename.split('\\n')] + file_contents[file_contents.index(line)+1:]
                       break
            if fc is not None:
                with open(nova_servers_file, 'w') as f:
                   f.writelines(fc)
    elif version == 'juno':
        with open(nova_servers_file, 'r') as f:
            file_contents = f.readlines()
        patched = False
        for line in file_contents:
            if 'from ovs.lib.vmachine import VMachineController' in line:
                patched = True
                break
        defupdate = False
        if not patched:
            for line in file_contents[:]:
                if 'def update(self,' in line:
                    defupdate = True
                    continue
                if defupdate:
                    if 'instance = self.compute_api.get(ctxt, id,' in line:
                       fc = file_contents[:file_contents.index(line)-2] + ['\\n'] + [l+'\\n' for l in updatename.split('\\n')] + file_contents[file_contents.index(line)-2:]
                       break
            if fc is not None:
                with open(nova_servers_file, 'w') as f:
                   f.writelines(fc)
" """ % (version, nova_volume_file, nova_driver_file, nova_servers_file))

    def _delete_volume_type(self, volume_type_name):
        """
        Delete a cinder volume type, based on vpool name
        """
        if self.cinder_client:
            volume_types = self.cinder_client.volume_types.list()
            for v in volume_types:
                if v.name == volume_type_name:
                    try:
                        self.cinder_client.volume_types.delete(v.id)
                    except Exception:
                        pass

    @staticmethod
    def _get_base_path(component):
        exec('import %s' % component, locals())
        module = locals().get(component)
        return os.path.dirname(os.path.abspath(module.__file__))
