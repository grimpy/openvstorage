# Copyright (C) 2018 iNuron NV
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
VDisk Constants module. Contains constants related to vdisks
"""

# General
LOCK_NAMESPACE = 'ovs_locks'

# Scrub related
SCRUB_VDISK_LOCK = '{0}_{{0}}'.format(LOCK_NAMESPACE)  # Second format is the vdisk guid
SCRUB_VDISK_EXCEPTION_MESSAGE = 'VDisk is being scrubbed. Unable to remove snapshots at this time'
