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

"""
CephProxy module
"""
from ovs.dal.dataobject import DataObject
from ovs.dal.structures import Relation
from ovs.dal.hybrids.storagedriver import StorageDriver
from ovs.dal.hybrids.service import Service


class CephProxy(DataObject):
    """
    The CephProxy class represents the junction table between the (ceph)Service and VPool.
    Examples:
    * my_vpool.ceph_proxies[0].service
    * my_service.ceph_proxy.vpool
    """
    __properties = []
    __relations = [Relation('storagedriver', StorageDriver, 'ceph_proxy', onetoone=True),
                   Relation('service', Service, 'ceph_proxy', onetoone=True)]
    __dynamics = []
