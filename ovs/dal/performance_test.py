#!/usr/bin/env python2
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
Performance unittest module
"""
import time
import sys
from ovs.dal.hybrids.t_testdisk import TestDisk
from ovs.dal.hybrids.t_testmachine import TestMachine
from ovs.dal.datalist import DataList
from ovs.dal.dataobject import DataObject
from ovs.dal.exceptions import ObjectNotFoundException
from ovs.dal.helpers import Toolbox
from ovs.extensions.storage.persistentfactory import PersistentFactory


class LotsOfObjects(object):
    """
    Executes a performance test by working with a large set of objects
    """
    def __init__(self):
        """
        Init method
        """
        self.persistent = PersistentFactory.get_client()

    def test_lotsofobjects(self):
        """
        A test creating, linking and querying a lot of objects
        """
        print ''
        print 'cleaning up'
        self._clean_all()
        print 'start test'
        tstart = time.time()
        if getattr(LotsOfObjects, 'amount_of_machines', None) is None:
            LotsOfObjects.amount_of_machines = 50
        if getattr(LotsOfObjects, 'amount_of_disks', None) is None:
            LotsOfObjects.amount_of_disks = 5
        load_data = True
        mguids = []
        if load_data:
            print '\nstart loading data'
            start = time.time()
            runtimes = []
            for i in xrange(0, int(LotsOfObjects.amount_of_machines)):
                mstart = time.time()
                machine = TestMachine()
                machine.name = 'machine_{0}'.format(i)
                machine.save()
                mguids.append(machine.guid)
                for ii in xrange(0, int(LotsOfObjects.amount_of_disks)):
                    disk = TestDisk()
                    disk.name = 'disk_{0}_{1}'.format(i, ii)
                    disk.size = ii * 100
                    disk.machine = machine
                    disk.save()
                avgitemspersec = ((i + 1) * LotsOfObjects.amount_of_disks) / (time.time() - start)
                itemspersec = LotsOfObjects.amount_of_disks / (time.time() - mstart)
                runtimes.append(itemspersec)
                LotsOfObjects._print_progress('* machine {0}/{1} (run: {2} dps, avg: {3} dps)'.format(i + 1, int(LotsOfObjects.amount_of_machines), round(itemspersec, 2), round(avgitemspersec, 2)))
            runtimes.sort()
            print '\nloading done ({0}s). min: {1} dps, max: {2} dps'.format(round(time.time() - tstart, 2), round(runtimes[1], 2), round(runtimes[-2], 2))

        test_queries = True
        if test_queries:
            print '\nstart queries'
            start = time.time()
            runtimes = []
            for i in xrange(0, int(LotsOfObjects.amount_of_machines)):
                mstart = time.time()
                machine = TestMachine(mguids[i])
                assert len(machine.disks) == LotsOfObjects.amount_of_disks, 'Not all disks were retrieved ({0})'.format(len(machine.disks))
                avgitemspersec = ((i + 1) * LotsOfObjects.amount_of_disks) / (time.time() - start)
                itemspersec = LotsOfObjects.amount_of_disks / (time.time() - mstart)
                runtimes.append(itemspersec)
                LotsOfObjects._print_progress('* machine {0}/{1} (run: {2} dps, avg: {3} dps)'.format(i + 1, int(LotsOfObjects.amount_of_machines), round(itemspersec, 2), round(avgitemspersec, 2)))
            runtimes.sort()
            print '\ncompleted ({0}s). min: {1} dps, max: {2} dps'.format(round(time.time() - tstart, 2), round(runtimes[1], 2), round(runtimes[-2], 2))

            print '\nstart full query on disk property'
            start = time.time()
            dlist = DataList(TestDisk, {'type': DataList.where_operator.AND,
                                        'items': [('size', DataList.operator.GT, 100),
                                                  ('size', DataList.operator.LT, (LotsOfObjects.amount_of_disks - 1) * 100)]})
            amount = len(dlist)
            assert amount == (LotsOfObjects.amount_of_disks - 3) * LotsOfObjects.amount_of_machines, 'Incorrect amount of disks. Found {0} instead of {1}'.format(amount, int((LotsOfObjects.amount_of_disks - 3) * LotsOfObjects.amount_of_machines))
            seconds_passed = (time.time() - start)
            print 'completed ({0}s) in {1} seconds (avg: {2} dps)'.format(round(time.time() - tstart, 2), round(seconds_passed, 2), round(LotsOfObjects.amount_of_machines * LotsOfObjects.amount_of_disks / seconds_passed, 2))

            print '\nloading disks (all)'
            start = time.time()
            for i in xrange(0, int(LotsOfObjects.amount_of_machines)):
                machine = TestMachine(mguids[i])
                _ = [_ for _ in machine.disks]
            seconds_passed = (time.time() - start)
            print 'completed ({0}s) in {1} seconds (avg: {2} dps)'.format(round(time.time() - tstart, 2), round(seconds_passed, 2), round(LotsOfObjects.amount_of_machines * LotsOfObjects.amount_of_disks / seconds_passed, 2))

            print '\nstart full query on disk property (using cached objects)'
            dlist._volatile.delete(dlist._key)
            start = time.time()
            dlist = DataList(TestDisk, {'type': DataList.where_operator.AND,
                                        'items': [('size', DataList.operator.GT, 100),
                                                  ('size', DataList.operator.LT, (LotsOfObjects.amount_of_disks - 1) * 100)]})
            amount = len(dlist)
            assert amount == (LotsOfObjects.amount_of_disks - 3) * LotsOfObjects.amount_of_machines, 'Incorrect amount of disks. Found {0} instead of {1}'.format(amount, int((LotsOfObjects.amount_of_disks - 3) * LotsOfObjects.amount_of_machines))
            seconds_passed = (time.time() - start)
            print 'completed ({0}s) in {1} seconds (avg: {2} dps)'.format(round(time.time() - tstart, 2), round(seconds_passed, 2), round(LotsOfObjects.amount_of_machines * LotsOfObjects.amount_of_disks / seconds_passed, 2))

            print '\nstart property sort'
            dlist = DataList(TestDisk, {'type': DataList.where_operator.AND,
                                        'items': []})
            start = time.time()
            dlist.sort(key=lambda a: Toolbox.extract_key(a, 'size'))
            seconds_passed = (time.time() - start)
            print 'completed ({0}s) in {1} seconds (avg: {2} dps)'.format(round(time.time() - tstart, 2), round(seconds_passed, 2), round(LotsOfObjects.amount_of_machines * LotsOfObjects.amount_of_disks / seconds_passed, 2))

            print '\nstart dynamic sort'
            dlist._volatile.delete(dlist._key)
            dlist = DataList(TestDisk, {'type': DataList.where_operator.AND,
                                        'items': []})
            start = time.time()
            dlist.sort(key=lambda a: Toolbox.extract_key(a, 'predictable'))
            seconds_passed = (time.time() - start)
            print 'completed ({0}s) in {1} seconds (avg: {2} dps)'.format(round(time.time() - tstart, 2), round(seconds_passed, 2), round(LotsOfObjects.amount_of_machines * LotsOfObjects.amount_of_disks / seconds_passed, 2))

        clean_data = True
        if clean_data:
            print '\ncleaning up'
            start = time.time()
            runtimes = []
            for i in xrange(0, int(LotsOfObjects.amount_of_machines)):
                mstart = time.time()
                machine = TestMachine(mguids[i])
                for disk in machine.disks:
                    disk.delete()
                machine.delete()
                avgitemspersec = ((i + 1) * LotsOfObjects.amount_of_disks) / (time.time() - start)
                itemspersec = LotsOfObjects.amount_of_disks / (time.time() - mstart)
                runtimes.append(itemspersec)
                LotsOfObjects._print_progress('* machine {0}/{1} (run: {2} dps, avg: {3} dps)'.format(i + 1, int(LotsOfObjects.amount_of_machines), round(itemspersec, 2), round(avgitemspersec, 2)))
            runtimes.sort()
            print '\ncompleted ({0}s). min: {1} dps, max: {2} dps'.format(round(time.time() - tstart, 2), round(runtimes[1], 2), round(runtimes[-2], 2))

    @staticmethod
    def _print_progress(message):
        """
        Prints progress (overwriting)
        """
        sys.stdout.write('\r{0}    '.format(message))
        sys.stdout.flush()

    def _clean_all(self):
        """
        Cleans all disks and machines
        """
        machine = TestMachine()
        prefix = '{0}_{1}_'.format(DataObject.NAMESPACE, machine._classname)
        keys = self.persistent.prefix(prefix)
        for key in keys:
            try:
                guid = key.replace(prefix, '')
                machine = TestMachine(guid)
                for disk in machine.disks:
                    disk.delete()
                machine.delete()
            except (ObjectNotFoundException, ValueError):
                pass
        for key in self.persistent.prefix('ovs_reverseindex_{0}'.format(machine._classname)):
            self.persistent.delete(key)
        disk = TestDisk()
        prefix = '{0}_{1}_'.format(DataObject.NAMESPACE, disk._classname)
        keys = self.persistent.prefix(prefix)
        for key in keys:
            try:
                guid = key.replace(prefix, '')
                disk = TestDisk(guid)
                disk.delete()
            except (ObjectNotFoundException, ValueError):
                pass
        for key in self.persistent.prefix('ovs_reverseindex_{0}'.format(disk._classname)):
            self.persistent.delete(key)

if __name__ == '__main__':
    if len(sys.argv) >= 3:
        LotsOfObjects.amount_of_machines = float(sys.argv[1])
        LotsOfObjects.amount_of_disks = float(sys.argv[2])
    performance = LotsOfObjects()
    performance.test_lotsofobjects()
