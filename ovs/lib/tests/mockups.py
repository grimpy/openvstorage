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
Mockups module
"""


class SRClient(object):
    """
    Mocks the SRClient
    """

    def __init__(self):
        """
        Dummy init method
        """
        pass

    @staticmethod
    def info_volume(volume_id):
        """
        Info volume mockup
        """
        return type('Info', (), {'object_type': property(lambda s: 'BASE'),
                                 'metadata_backend_config': property(lambda s: StorageDriverClient.metadata_backend_config.get(volume_id)),
                                 'vrouter_id': property(lambda s: StorageDriverClient.vrouter_id.get(volume_id))})()

    @staticmethod
    def update_metadata_backend_config(volume_id, metadata_backend_config):
        """
        Stores the given config
        """
        StorageDriverClient.metadata_backend_config[volume_id] = metadata_backend_config


class StorageDriverClient(object):
    """
    Mocks the StorageDriverClient
    """

    snapshots = {}
    metadata_backend_config = {}
    catch_up = {}
    vrouter_id = {}

    def __init__(self):
        """
        Dummy init method
        """
        pass

    @staticmethod
    def load(vpool):
        """
        Returns the mocked SRClient
        """
        _ = vpool
        return SRClient()

    @staticmethod
    def empty_info():
        """
        Returns an empty info object
        """
        return type('Info', (), {'object_type': property(lambda s: 'BASE'),
                                 'metadata_backend_config': property(lambda s: None),
                                 'vrouter_id': property(lambda s: None)})()


class MDSClient(object):
    """
    Mocks the MDSClient
    """

    def __init__(self, service):
        """
        Dummy init method
        """
        self.service = service

    def catch_up(self, volume_id, dry_run):
        """
        Dummy catchup
        """
        _ = self, dry_run
        return StorageDriverClient.catch_up[volume_id]

    def create_namespace(self, volume_id):
        """
        Dummy create namespace method
        """
        _ = self, volume_id


class MetadataServerClient(object):
    """
    Mocks the MetadataServerClient
    """

    mds_data = {}

    def __init__(self):
        """
        Dummy init method
        """
        pass

    @staticmethod
    def load(service):
        """
        Returns the mocked MDSClient
        """
        return MDSClient(service)


class StorageDriverConfiguration(object):
    """
    Mocks the StorageDriverConfiguration
    """

    def __init__(self):
        """
        Dummy init method
        """
        pass


class StorageDriverModule(object):
    """
    Mocks the StorageDriver
    """
    StorageDriverClient = StorageDriverClient
    MetadataServerClient = MetadataServerClient
    StorageDriverConfiguration = StorageDriverConfiguration

    def __init__(self):
        """
        Dummy init method
        """
        pass
