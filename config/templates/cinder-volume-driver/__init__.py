# Copyright 2014 iNuron NV
#
# Licensed under the Open vStorage Non-Commercial License, Version 1.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.openvstorage.org/OVS_NON_COMMERCIAL
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This package contains the openstack cinder driver

Tested on devstack
/opt/devstack# cinder --version
1.0.9.23
/opt/devstack# nova --version
2.18.0.8
/opt/devstack# glance --version
0.13.1.6

Copy the ovs_volume_driver.py to /opt/stack/cinder/cinder/volume/drivers/ovs_volume_driver.py
"""
