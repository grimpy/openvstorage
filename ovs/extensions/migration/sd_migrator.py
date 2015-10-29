# Copyright 2015 Open vStorage NV
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

import os
import copy
from ovs.extensions.generic.configuration import Configuration


class StorageDriverMigrator(object):
    def __init__(self):
        pass

    @staticmethod
    def migrate():
        """
        Migrates from any version to any version, running all migrations required
        If previous_version is for example 0 and this script is at
        version 3 it will execute two steps:
          - 1 > 2
          - 2 > 3
        """

        data = Configuration.get('ovs.core.versions') if Configuration.exists('ovs.core.versions') else {}
        identifier = 'storagedriver'
        working_version = data[identifier] if identifier in data else 0

        # Version 1 introduced:
        # - New backend_connection_manager format
        if working_version < 1:
            from ovs.extensions.storageserver.storagedriver import StorageDriverConfiguration
            configuration_dir = '{0}/storagedriver/storagedriver'.format(Configuration.get('ovs.core.cfgdir'))
            if not os.path.exists(configuration_dir):
                os.makedirs(configuration_dir)
            for json_file in os.listdir(configuration_dir):
                vpool_name = json_file.replace('.json', '')
                if json_file.endswith('.json'):
                    if os.path.exists('{0}/{1}.cfg'.format(configuration_dir, vpool_name)):
                        continue  # There's also a .cfg file, so this is an alba_proxy configuration file
                    storagedriver_config = StorageDriverConfiguration('storagedriver', vpool_name)
                    storagedriver_config.load()
                    existing_config = storagedriver_config.configuration['backend_connection_manager']
                    if existing_config['backend_type'] == 'ALBA':
                        # double check to make sure we don't overwrite other backends
                        # also don't overwrite already updated configs
                        config = copy.deepcopy(existing_config)
                        storagedriver_config.clean()
                        storagedriver_config.configure_backend_connection_manager(backend_type='OVSPROXY',
                                                                                  ovs_proxy_connection_type='ALBA',
                                                                                  ovs_proxy_connection_host=config['alba_connection_host'],
                                                                                  ovs_proxy_connection_port=config['alba_connection_port'],
                                                                                  ovs_proxy_connection_preset=config['alba_connection_preset'])
                    storagedriver_config.save(reload_config=False)
            working_version = 1

        # Version 2 introduced
        # - ...
        # if working_version < 2:
        #     ...
        #     working_version = 2

        data[identifier] = working_version
        Configuration.set('ovs.core.versions', data)
