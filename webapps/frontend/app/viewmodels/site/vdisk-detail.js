// Copyright (C) 2016 iNuron NV
//
// This file is part of Open vStorage Open Source Edition (OSE),
// as available from
//
//      http://www.openvstorage.org and
//      http://www.openvstorage.com.
//
// This file is free software; you can redistribute it and/or modify it
// under the terms of the GNU Affero General Public License v3 (GNU AGPLv3)
// as published by the Free Software Foundation, in version 3 as it comes
// in the LICENSE.txt file of the Open vStorage OSE distribution.
//
// Open vStorage is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY of any kind.
/*global define */
define([
    'jquery', 'durandal/app', 'plugins/dialog', 'knockout', 'plugins/router',
    'ovs/shared', 'ovs/generic', 'ovs/refresher', 'ovs/api',
    '../containers/vdisk', '../containers/vpool', '../containers/storagerouter', '../containers/domain',
    '../wizards/clone/index', '../wizards/vdiskmove/index', '../wizards/rollback/index', '../wizards/snapshot/index',
    '../wizards/scrub/index'
], function(
    $, app, dialog, ko, router, shared, generic, Refresher, api,
    VDisk, VPool, StorageRouter, Domain,
    CloneWizard, MoveWizard, RollbackWizard, SnapshotWizard, ScrubWizard
) {
    "use strict";
    return function() {
        var self = this;

        // Variables
        self.domainCache      = {};
        self.shared           = shared;
        self.guard            = { authenticated: true };
        self.refresher        = new Refresher();
        self.widgets          = [];
        self.snapshotHeaders  = [
            { key: 'label',         value: $.t('ovs:generic.description'), width: undefined },
            { key: 'timestamp',     value: $.t('ovs:generic.datetime'),    width: 200       },
            { key: 'stored',        value: $.t('ovs:generic.storeddata'),  width: 110       },
            { key: 'is_automatic',  value: $.t('ovs:generic.type'),        width: 110       },
            { key: 'is_consistent', value: $.t('ovs:generic.consistent'),  width: 100       },
            { key: 'is_sticky',     value: $.t('ovs:generic.sticky'),      width: 100       },
            { key: undefined,       value: $.t('ovs:generic.actions'),     width: 60        }
        ];
        self.edgeClientHeaders = [
            { key: 'ip',    value: $.t('ovs:generic.ip'),    width: 200       },
            { key: 'port',  value: $.t('ovs:generic.port'),  width: undefined }
        ];

        // Observables
        self.convertingToTemplate = ko.observable(false);
        self.domains              = ko.observableArray([]);
        self.snapshotsInitialLoad = ko.observable(true);
        self.removing             = ko.observable(false);
        self.vDisk                = ko.observable();

        // Handles
        self.loadDomainHandle        = undefined;
        self.loadStorageRouterHandle = undefined;

        // Functions
        self.load = function() {
            return $.Deferred(function (deferred) {
                self.vDisk().load()
                    .then(function() {
                        if (self.vDisk().isVTemplate()) {
                            router.navigate(shared.routing.loadHash('vdisks'));
                            return deferred.reject();
                        }
                    })
                    .then(self.vDisk().fetchChildrenGuids)
                    .then(self.loadStorageRouters)
                    .then(self.loadDomains)
                    .then(function() {
                        self.snapshotsInitialLoad(false);
                        var sr, pool, vdisk = self.vDisk(),
                            storageRouterGuid = vdisk.storageRouterGuid(),
                            vPoolGuid = vdisk.vpoolGuid();
                        if (storageRouterGuid && (vdisk.storageRouter() === undefined || vdisk.storageRouter().guid() !== storageRouterGuid)) {
                            sr = new StorageRouter(storageRouterGuid);
                            sr.load();
                            vdisk.storageRouter(sr);
                        }
                        if (vPoolGuid && (vdisk.vpool() === undefined || vdisk.vpool().guid() !== vPoolGuid)) {
                            pool = new VPool(vPoolGuid);
                            pool.load('configuration');
                            vdisk.vpool(pool);
                        }
                    })
                    .fail(function(error) {
                        if (error !== undefined && error.status === 404) {
                            router.navigate(shared.routing.loadHash('vdisks'));
                        }
                    })
                    .always(deferred.resolve);
            }).promise();
        };
        self.loadDomains = function() {
            return $.Deferred(function(deferred) {
                var vdisk = self.vDisk();
                if (vdisk !== undefined && generic.xhrCompleted(self.loadDomainHandle)) {
                    self.loadDomainHandle = api.get('domains', { queryparams: { contents: 'storage_router_layout' }})
                        .done(function(data) {
                            var guids = [], ddata = {}, domainsPresent = false;
                            $.each(data.data, function(index, item) {
                                if (item.storage_router_layout.regular.contains(self.vDisk().storageRouterGuid())) {
                                    domainsPresent = true;
                                    if (item.storage_router_layout.regular.length > 1) {
                                        guids.push(item.guid);
                                        ddata[item.guid] = item;
                                    }
                                } else if (item.storage_router_layout.regular.length > 0)  {
                                    domainsPresent = true;
                                    guids.push(item.guid);
                                    ddata[item.guid] = item;
                                }
                            });
                            self.vDisk().domainsPresent(domainsPresent);
                            self.vDisk().dtlTargets(guids);
                            generic.crossFiller(
                                guids, self.domains,
                                function(guid) {
                                    return new Domain(guid);
                                }, 'guid'
                            );
                            $.each(self.domains(), function(index, domain) {
                                if (!self.domainCache.hasOwnProperty(domain.guid())) {
                                    self.domainCache[domain.guid()] = domain;
                                }
                                if (ddata.hasOwnProperty(domain.guid())) {
                                    domain.fillData(ddata[domain.guid()]);
                                }
                            });
                            deferred.resolve();
                        })
                        .fail(deferred.reject);
                } else {
                    deferred.resolve();
                }
            }).promise();
        };
        self.loadStorageRouters = function() {
            return $.Deferred(function (deferred) {
                if (generic.xhrCompleted(self.loadStorageRouterHandle)) {
                    self.loadStorageRouterHandle = api.get('storagerouters')
                        .done(function (data) {
                            self.vDisk().storageRouterGuids(data.data);
                        })
                        .always(deferred.resolve());
                }
            }).promise();
        };
        self.refreshSnapshots = function() {
            // Not in use, for mapping only
        };
        self.formatBytes = function(value) {
            return generic.formatBytes(value);
        };
        self.rollback = function() {
            if (self.vDisk() !== undefined) {
                dialog.show(new RollbackWizard({
                    modal: true,
                    guid: self.vDisk().guid()
                }));
            }
        };
        self.snapshot = function() {
            if (self.vDisk() !== undefined) {
                dialog.show(new SnapshotWizard({
                    modal: true,
                    guid: self.vDisk().guid()
                }));
            }
        };
        self.setAsTemplate = function() {
            if (self.vDisk() !== undefined && self.vDisk().childrenGuids().length === 0) {
                var vd = self.vDisk();
                self.convertingToTemplate(true);
                app.showMessage(
                        $.t('ovs:vdisks.set_as_template.warning'),
                        $.t('ovs:vdisks.set_as_template.title', {what: vd.name()}),
                        [$.t('ovs:vdisks.set_as_template.no'), $.t('ovs:vdisks.set_as_template.yes')]
                    )
                    .done(function(answer) {
                        if (answer === $.t('ovs:vdisks.set_as_template.yes')) {
                            generic.alertInfo(
                                $.t('ovs:vdisks.set_as_template.started'),
                                $.t('ovs:vdisks.set_as_template.started_msg', {what: vd.name()})
                            );
                            api.post('vdisks/' + vd.guid() + '/set_as_template')
                                .then(self.shared.tasks.wait)
                                .done(function() {
                                    generic.alertSuccess(
                                        $.t('ovs:vdisks.set_as_template.success'),
                                        $.t('ovs:vdisks.set_as_template.success_msg', {what: vd.name()})
                                    );
                                    router.navigate(shared.routing.loadHash('vtemplates'));
                                })
                                .fail(function(error) {
                                    error = generic.extractErrorMessage(error);
                                    generic.alertError(
                                        $.t('ovs:generic.error'),
                                        $.t('ovs:vdisks.set_as_template.failed_msg', {what: vd.name(), why: error})
                                    );
                                })
                                .always(function() {
                                    self.convertingToTemplate(false);
                                });
                        } else {
                            self.convertingToTemplate(false);
                        }
                    });
            }
        };
        self.clone = function() {
            if (self.vDisk() !== undefined) {
                dialog.show(new CloneWizard({
                    modal: true,
                    vdisk: self.vDisk()
                }));
            }
        };
        self.move = function() {
            if (self.vDisk() !== undefined) {
                dialog.show(new MoveWizard({
                    modal: true,
                    vdisk: self.vDisk()
                }));
            }
        };
        self.scrub = function() {
            if (self.vDisk() !== undefined) {
                dialog.show(new ScrubWizard({
                    modal: true,
                    vdisk: self.vDisk()
                }));
            }
        };
        self.saveConfiguration = function() {
            if (self.vDisk() !== undefined) {
                var vd = self.vDisk();
                api.post('vdisks/' + vd.guid() + '/set_config_params', {
                    data: { new_config_params: vd.configuration() }
                })
                    .then(self.shared.tasks.wait)
                    .done(function () {
                        generic.alertSuccess(
                            $.t('ovs:vdisks.save_config.success'),
                            $.t('ovs:vdisks.save_config.success_msg', {what: vd.name()})
                        );
                    })
                    .fail(function (error) {
                        error = generic.extractErrorMessage(error);
                        generic.alertError(
                            $.t('ovs:generic.error'),
                            $.t('ovs:vdisks.save_config.failed_msg', {what: vd.name(), why: error})
                        );
                    })
                    .always(function() {
                        vd.loadConfiguration(false);
                    });
                vd.oldConfiguration($.extend({}, vd.configuration()));
                vd.dtlTarget(vd.configuration().dtl_target.slice());
            }
        };
        self.removeSnapshot = function(snapshotid) {
            app.showMessage(
                $.t('ovs:vdisks.remove_snapshot.title_msg', {what: snapshotid}),
                $.t('ovs:generic.areyousure'),
                [$.t('ovs:generic.no'), $.t('ovs:generic.yes')]
            )
            .done(function(answer) {
                if (answer === $.t('ovs:generic.yes')) {
                    generic.alertInfo(
                        $.t('ovs:vdisks.remove_snapshot.started'),
                        $.t('ovs:vdisks.remove_snapshot.started_msg', {what: snapshotid})
                    );
                    api.post('vdisks/' + self.vDisk().guid() + '/remove_snapshot', {
                        data: { snapshot_id: snapshotid }
                    })
                        .then(self.shared.tasks.wait)
                        .done(function () {
                            generic.alertSuccess(
                                $.t('ovs:vdisks.remove_snapshot.success'),
                                $.t('ovs:vdisks.remove_snapshot.success_msg', {what: snapshotid})
                            );
                        })
                        .fail(function (error) {
                            error = generic.extractErrorMessage(error);
                            generic.alertError(
                                $.t('ovs:generic.error'),
                                $.t('ovs:vdisks.remove_snapshot.failed_msg', {what: snapshotid, why: error})
                            );
                        })
                        .always(function () {
                            self.load();
                        });
            }});
        };
        self.removeVDisk = function() {
            if (self.vDisk() !== undefined && self.vDisk().childrenGuids().length === 0) {
                var vd = self.vDisk();
                self.removing(true);
                app.showMessage(
                        $.t('ovs:vdisks.remove_vdisk.title_msg', {what: vd.name()}),
                        $.t('ovs:generic.areyousure'),
                        [$.t('ovs:generic.no'), $.t('ovs:generic.yes')]
                    )
                    .done(function(answer) {
                        if (answer === $.t('ovs:generic.yes')) {
                            generic.alertInfo(
                                $.t('ovs:vdisks.remove_vdisk.started'),
                                $.t('ovs:vdisks.remove_vdisk.started_msg', {what: vd.name()})
                            );
                            api.del('vdisks/' + vd.guid())
                                .then(self.shared.tasks.wait)
                                .done(function() {
                                    generic.alertSuccess(
                                        $.t('ovs:vdisks.remove_vdisk.success'),
                                        $.t('ovs:vdisks.remove_vdisk.success_msg', {what: vd.name()})
                                    );
                                    router.navigate(shared.routing.loadHash('vdisks'));
                                })
                                .fail(function(error) {
                                    error = generic.extractErrorMessage(error);
                                    generic.alertError(
                                        $.t('ovs:generic.error'),
                                        $.t('ovs:vdisks.remove_vdisk.failed_msg', {what: vd.name(), why: error})
                                    );
                                })
                                .always(function() {
                                    self.removing(false);
                                });
                        } else {
                            self.removing(false);
                        }
                    });
            }
        };

        // Computed
        self.canBeModified = ko.computed(function() {
            return self.convertingToTemplate() === false && self.removing() === false;
        });

        // Durandal
        self.activate = function(mode, guid) {
            self.vDisk(new VDisk(guid));
            self.refresher.init(self.load, 5000);
            self.refresher.run();
            self.refresher.start();
        };
        self.deactivate = function() {
            $.each(self.widgets, function(index, item) {
                item.deactivate();
            });
            self.refresher.stop();
        };
    };
});
