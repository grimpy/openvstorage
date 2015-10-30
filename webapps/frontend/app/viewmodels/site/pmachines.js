﻿// Copyright 2014 iNuron NV
//
// Licensed under the Open vStorage Non-Commercial License, Version 1.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.openvstorage.org/OVS_NON_COMMERCIAL
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
/*global define */
define([
    'jquery', 'durandal/app', 'plugins/dialog', 'knockout',
    'ovs/shared', 'ovs/generic', 'ovs/refresher', 'ovs/api',
    '../containers/pmachine', '../containers/mgmtcenter',
    '../wizards/addmgmtcenter/index',
    '../wizards/linkhosts/index'
], function($, app, dialog, ko, shared, generic, Refresher, api, PMachine, MgmtCenter, AddMgmtCenterWizard, LinkHostsWizard) {
    "use strict";
    return function() {
        var self = this;

        // Variables
        self.shared            = shared;
        self.holdLoading       = false;
        self.guard             = { authenticated: true };
        self.refresher         = new Refresher();
        self.widgets           = [];
        self.mgmtCenterHeaders = [
            { key: 'name',      value: $.t('ovs:generic.name'),               width: 250       },
            { key: 'ipAddress', value: $.t('ovs:generic.ip'),                 width: 150       },
            { key: 'port',      value: $.t('ovs:generic.port'),               width: 60        },
            { key: 'type',      value: $.t('ovs:generic.type'),               width: 150       },
            { key: 'username',  value: $.t('ovs:generic.username'),           width: 150       },
            { key: undefined,   value: $.t('ovs:pmachines.mgmtcenter.hosts'), width: undefined },
            { key: undefined,   value: $.t('ovs:generic.actions'),            width: 60        }
        ];
        self.pMachineHeaders   = [
            { key: 'name',            value: $.t('ovs:generic.name'),       width: 250       },
            { key: 'ipAddress',       value: $.t('ovs:generic.ip'),         width: 150       },
            { key: 'hvtype',          value: $.t('ovs:generic.type'),       width: 210       },
            { key: 'mgmtcenter_guid', value: $.t('ovs:generic.mgmtcenter'), width: undefined },
            { key: undefined,         value: $.t('ovs:generic.actions'),    width: 60        }
        ];

        // Observables
        self.pMachines         = ko.observableArray([]);
        self.mgmtCenters       = ko.observableArray([]);
        self.mgmtCenterMapping = ko.observable({});

        // Handles
        self.pMachinesHandle   = {};
        self.mgmtCentersHandle = {};

        // Computed
        self.hostMapping = ko.computed(function() {
           var pMachineIPs = [], mapping = {}, match = false;
            $.each(self.pMachines(), function(pindex, pMachine) {
                pMachineIPs.push(pMachine.ipAddress());
            });
            $.each(self.mgmtCenters(), function(mindex, mgmtCenter) {
                mapping[mgmtCenter.guid()] = {
                    ovs: 0,
                    total: 0
                };
                $.each(mgmtCenter.hosts(), function(hindex, host) {
                    mapping[mgmtCenter.guid()].total += 1;
                    match = false;
                    $.each(host.ips, function(iindex, ip) {
                        if (pMachineIPs.contains(ip)) {
                            match = true;
                            return false;
                        }
                        return true;
                    });
                    if (match) {
                        mapping[mgmtCenter.guid()].ovs += 1;
                    }
                });
            });
            return mapping;
        });
        self.mgmtCenterChoices = ko.computed(function() {
            var centers = self.mgmtCenters().slice();
            centers.push(undefined);
            return centers;
        });

        // Functions
        self.loadPMachines = function(page) {
            return $.Deferred(function(deferred) {
                if (self.holdLoading === true) {
                    deferred.resolve();
                    return;
                }
                if (generic.xhrCompleted(self.pMachinesHandle[page])) {
                    var options = {
                        sort: 'name',
                        contents: 'mgmtcenter',
                        page: page
                    };
                    self.pMachinesHandle[page] = api.get('pmachines', { queryparams: options })
                        .done(function(data) {
                            deferred.resolve({
                                data: data,
                                loader: function(guid) {
                                    var pm = new PMachine(guid);
                                    pm.configure = ko.observable(true);
                                    pm.originalMgmtCenterGuid = ko.observable();
                                    pm.mgmtCenter = ko.computed({
                                        write: function(mgmtCenter) {
                                            if (mgmtCenter === undefined) {
                                                this.mgmtCenterGuid(undefined);
                                            } else {
                                                this.mgmtCenterGuid(mgmtCenter.guid());
                                            }
                                        },
                                        read: function() {
                                            if (self.mgmtCenterMapping().hasOwnProperty(this.mgmtCenterGuid())) {
                                                return self.mgmtCenterMapping()[this.mgmtCenterGuid()];
                                            }
                                            return undefined;
                                        },
                                        owner: pm
                                    });
                                    pm.missingMgmtCenter = ko.computed({
                                        read: function() {
                                            var pmachine = this,
                                                missing = false;
                                            $.each(self.mgmtCenters(), function(mcindex, mgmtCenter) {
                                                $.each(mgmtCenter.hosts(), function(hindex, host) {
                                                    $.each(host.ips, function(iindex, ip) {
                                                        if (ip === pmachine.ipAddress() && (pmachine.mgmtCenterGuid() === null || pmachine.mgmtCenterGuid() === undefined)) {
                                                            missing = true;
                                                            return false;
                                                        }
                                                        return true;
                                                    });
                                                });
                                            });
                                            return missing;
                                        },
                                        owner: pm
                                    });
                                    pm.mgmtCenterValid = ko.computed({
                                        read: function() {
                                            // Currently, matching is based on ip address
                                            // TODO: Replace this by hypervisorid matching
                                            if (pm.missingMgmtCenter()) {
                                                return true;
                                            }
                                            var mgmtCenterGuid, pmachine = this,
                                                currentMgmtCenterGuid = pmachine.mgmtCenterGuid();
                                            $.each(self.mgmtCenters(), function(mcindex, mgmtCenter) {
                                                $.each(mgmtCenter.hosts(), function(hindex, host) {
                                                    $.each(host.ips, function(iindex, ip) {
                                                        if (ip === pmachine.ipAddress()) {
                                                            mgmtCenterGuid = mgmtCenter.guid();
                                                            return false;
                                                        }
                                                        return true;
                                                    });
                                                    return mgmtCenterGuid === undefined;
                                                });
                                                return mgmtCenterGuid === undefined;
                                            });
                                            if (mgmtCenterGuid === undefined && (currentMgmtCenterGuid === null || currentMgmtCenterGuid === undefined)) {
                                                return true;
                                            }
                                            return mgmtCenterGuid === pmachine.mgmtCenterGuid();
                                        },
                                        owner: pm
                                    });
                                    return pm;
                                },
                                dependencyLoader: function(pmachine) {
                                    if (pmachine.originalMgmtCenterGuid() === undefined || pmachine.originalMgmtCenterGuid() === null) {
                                        pmachine.originalMgmtCenterGuid(pmachine.mgmtCenterGuid());
                                    }
                                    pmachine.loadHostConfigurationState();
                                }
                            });
                        })
                        .fail(function() { deferred.reject(); });
                } else {
                    deferred.resolve();
                }
            }).promise();
        };
        self.loadMgmtCenters = function(page) {
            return $.Deferred(function(deferred) {
                if (generic.xhrCompleted(self.mgmtCentersHandle[page])) {
                    var options = {
                        sort: 'name',
                        contents: 'hosts',
                        page: page
                    };
                    self.mgmtCentersHandle[page] = api.get('mgmtcenters', { queryparams: options })
                        .done(function(data) {
                            deferred.resolve({
                                data: data,
                                loader: function(guid) {
                                    var mc = new MgmtCenter(guid),
                                        mapping = self.mgmtCenterMapping();

                                    if (!mapping.hasOwnProperty(guid)) {
                                        mapping[guid] = mc;
                                        self.mgmtCenterMapping(mapping);
                                    }
                                    return mc;
                                }
                            });
                        })
                        .fail(function() { deferred.reject(); });
                } else {
                    deferred.resolve();
                }
            }).promise();
        };
        self.addMgmtCenter = function() {
            dialog.show(new AddMgmtCenterWizard({
                modal: true
            }));
        };
        self.linkHosts = function() {
            self.holdLoading = true;
            var wizard = dialog.show(new LinkHostsWizard({
                modal: true,
                pmachines: self.pMachines(),
                mgmtcenters: self.mgmtCenters()
            }));
            wizard.always(function() {
                self.holdLoading = false;
            })
        };
        self.reconfigureHost = function(guid, configure) {
            var pmachine;
            $.each(self.pMachines(), function(index, pm) {
                if (pm.guid() === guid) {
                    pmachine = pm;
                }
            });
            if (pmachine !== undefined) {
                app.showMessage(
                    $.t('ovs:pmachines.configure.host.warning', { what: configure === true ? 'configure' : 'unconfigure', which: pmachine.name() }),
                    $.t('ovs:generic.areyousure'),
                    [$.t('ovs:generic.no'), $.t('ovs:generic.yes')]
                )
                .done(function(answer) {
                    if (answer === $.t('ovs:generic.yes')) {
                        var action = configure === true ? '/configure_host' : '/unconfigure_host';
                        api.post('pmachines/' + pmachine.guid() + action, {
                            data: {
                                update_link: false,
                                mgmtcenter_guid: pmachine.mgmtCenter() === undefined ? null : pmachine.mgmtCenter().guid()
                            }
                        })
                        .then(shared.tasks.wait)
                        .done(function() {
                            generic.alertSuccess(
                                $.t('ovs:wizards.linkhosts.gather.completed', { which: (action === '/configure_host' ? 'Configure' : 'Unconfigure')}),
                                $.t('ovs:wizards.linkhosts.gather.success', { which: (action === '/configure_host' ? 'configured' : 'unconfigured'), what: pmachine.name() })
                            );
                        })
                        .fail(function(error) {
                            generic.alertError(
                                $.t('ovs:generic.error'),
                                $.t('ovs:wizards.linkhosts.gather.failed', {
                                    which: (action === '/configure_host' ? 'Configuring' : 'Unconfiguring'),
                                    what: pmachine.name(),
                                    why: error
                                })
                            );
                        })
                    }
                });
            }
        }
        self.deleteMgmtCenter = function(guid) {
            var mgmtCenter;
            $.each(self.mgmtCenters(), function(index, mc) {
                if (mc.guid() === guid) {
                    mgmtCenter = mc;
                }
            });
            if (mgmtCenter !== undefined) {
                app.showMessage(
                    $.t('ovs:pmachines.delete.warning', { what: mgmtCenter.name() }),
                    $.t('ovs:generic.areyousure'),
                    [$.t('ovs:generic.no'), $.t('ovs:generic.yes')]
                )
                .done(function(answer) {
                    if (answer === $.t('ovs:generic.yes')) {
                        self.mgmtCenters.destroy(mgmtCenter);
                        generic.alertInfo(
                            $.t('ovs:pmachines.delete.marked'),
                            $.t('ovs:pmachines.delete.markedmsg', { what: mgmtCenter.name() })
                        );
                        api.del('mgmtcenters/' + mgmtCenter.guid())
                            .done(function() {
                                generic.alertSuccess(
                                    $.t('ovs:pmachines.delete.done'),
                                    $.t('ovs:pmachines.delete.donemsg', { what: mgmtCenter.name() })
                                );
                            })
                            .fail(function(error) {
                                generic.alertError(
                                    $.t('ovs:generic.error'),
                                    $.t('ovs:generic.messages.errorwhile', {
                                        context: 'error',
                                        what: $.t('ovs:pmachines.delete.errormsg', { what: mgmtCenter.name() }),
                                        error: error.responseText
                                    })
                                );
                            });
                    }
                });
            }
        };
    };
});
