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
define(['jquery', 'knockout'], function($, ko){
    "use strict";
    var nameRegex, hostRegex, ipRegex, singleton, parsePresets;
    nameRegex = /^[0-9a-z][\-a-z0-9]{1,20}[a-z0-9]$/;
    hostRegex = /^((((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?))|((([a-z0-9]+[\.\-])*[a-z0-9]+\.)+[a-z]{2,4}))$/;
    ipRegex = /^(((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?))$/;

    parsePresets = function(backend) {
        var presets = [], policies, newPolicy, isAvailable, isActive, inUse,
            policyMapping = ['grey', 'black', 'green'], worstPolicy, replication, policyObject;
        $.each(backend.presets, function(index, preset) {
            worstPolicy = 0;
            policies = [];
            replication = undefined;
            $.each(preset.policies, function(jndex, policy) {
                policyObject = JSON.parse(policy.replace('(', '[').replace(')', ']'));
                isAvailable = preset.policy_metadata[policy].is_available;
                isActive = preset.policy_metadata[policy].is_active;
                inUse = preset.policy_metadata[policy].in_use;
                newPolicy = {
                    text: policy,
                    color: 'grey',
                    isActive: false,
                    k: policyObject[0],
                    m: policyObject[1],
                    c: policyObject[2],
                    x: policyObject[3]
                };
                if (isAvailable) {
                    newPolicy.color = 'black';
                }
                if (isActive) {
                    newPolicy.isActive = true;
                }
                if (inUse) {
                    newPolicy.color = 'green';
                }
                worstPolicy = Math.max(policyMapping.indexOf(newPolicy.color), worstPolicy);
                policies.push(newPolicy);
            });
            if (preset.policies.length === 1) {
                policyObject = JSON.parse(preset.policies[0].replace('(', '[').replace(')', ']'));
                if (policyObject[0] === 1 && policyObject[0] + policyObject[1] === policyObject[3] && policyObject[2] === 1) {
                    replication = policyObject[0] + policyObject[1];
                }
            }
            presets.push({
                policies: policies,
                name: preset.name,
                compression: preset.compression,
                fragSize: preset.fragment_size,
                encryption: preset.fragment_encryption,
                color: policyMapping[worstPolicy],
                inUse: preset.in_use,
                isDefault: preset.is_default,
                replication: replication
            });
        });
        return presets.sort(function(preset1, preset2) {
            return preset1.name.toLowerCase() < preset2.name.toLowerCase() ? -1 : 1;
        });
    };

    singleton = function() {
        var wizardData = {
            // 'aa' stands for Accelerated Alba
            backend:                 ko.observable(),
            backendAA:               ko.observable(),
            backends:                ko.observableArray([]),
            backendsAA:              ko.observableArray([]),
            clientID:                ko.observable('').extend({removeWhiteSpaces: null}),
            clientIDAA:              ko.observable('').extend({removeWhiteSpaces: null}),
            clientSecret:            ko.observable('').extend({removeWhiteSpaces: null}),
            clientSecretAA:          ko.observable('').extend({removeWhiteSpaces: null}),
            clusterSize:             ko.observable(4),
            clusterSizes:            ko.observableArray([4, 8, 16, 32, 64]),
            dtlEnabled:              ko.observable(true),
            dtlMode:                 ko.observable(),
            dtlModes:                ko.observableArray([{name: 'no_sync', disabled: false}, {name: 'a_sync', disabled: false}, {name: 'sync', disabled: false}]),
            dtlTransportMode:        ko.observable({name: 'tcp'}),
            dtlTransportModes:       ko.observableArray([{name: 'tcp', disabled: false}, {name: 'rdma', disabled: true}]),
            fragmentCacheOnRead:     ko.observable(true),
            fragmentCacheOnWrite:    ko.observable(true),
            host:                    ko.observable('').extend({regex: hostRegex}),
            hostAA:                  ko.observable('').extend({regex: hostRegex}),
            ipAddresses:             ko.observableArray([]),
            localHost:               ko.observable(true),
            localHostAA:             ko.observable(true),
            name:                    ko.observable('').extend({regex: nameRegex}),
            partitions:              ko.observable(),
            port:                    ko.observable(80).extend({ numeric: {min: 1, max: 65536}}),
            portAA:                  ko.observable(80).extend({numeric: {min: 1, max: 65536}}),
            preset:                  ko.observable(),
            presetAA:                ko.observable(),
            reUsedStorageRouter:     ko.observable(),  // Connection info for this storagerouter will be used for accelerated ALBA
            scrubAvailable:          ko.observable(false),
            scoSize:                 ko.observable(4),
            scoSizes:                ko.observableArray([4, 8, 16, 32, 64, 128]),
            storageIP:               ko.observable().extend({regex: ipRegex, identifier: 'storageip'}),
            storageRouter:           ko.observable(),
            storageRoutersAvailable: ko.observableArray([]),
            storageRoutersUsed:      ko.observableArray([]),
            useAA:                   ko.observable(false),
            vPool:                   ko.observable(),
            vPools:                  ko.observableArray([]),
            writeBufferGlobal:       ko.observable().extend({numeric: {min: 1, max: 10240, allowUndefined: true}}),
            writeBufferGlobalMax:    ko.observable(),
            writeBufferVolume:       ko.observable(128).extend({numeric: {min: 128, max: 10240}})
        }, resetBackends = function() {
            wizardData.backends([]);
            wizardData.backend(undefined);
            wizardData.preset(undefined);
        }, resetBackendsAA = function() {
            wizardData.backendsAA([]);
            wizardData.backendAA(undefined);
            wizardData.presetAA(undefined);
        };

        wizardData.clientID.subscribe(resetBackends);
        wizardData.clientSecret.subscribe(resetBackends);
        wizardData.host.subscribe(resetBackends);
        wizardData.port.subscribe(resetBackends);
        wizardData.localHost.subscribe(function() {
            wizardData.host('');
            wizardData.port(80);
            wizardData.clientID('');
            wizardData.clientSecret('');
            resetBackends();
        });
        wizardData.hostAA.subscribe(resetBackendsAA);
        wizardData.portAA.subscribe(resetBackendsAA);
        wizardData.clientIDAA.subscribe(resetBackendsAA);
        wizardData.clientSecretAA.subscribe(resetBackendsAA);
        wizardData.localHostAA.subscribe(function() {
            wizardData.hostAA('');
            wizardData.portAA(80);
            wizardData.clientIDAA('');
            wizardData.clientSecretAA('');
            wizardData.reUsedStorageRouter(undefined);
            resetBackendsAA();
        });
        wizardData.scoSize.subscribe(function(size) {
            if (size < 128) {
                wizardData.writeBufferVolume.min = 128;
            } else {
                wizardData.writeBufferVolume.min = 256;
            }
            wizardData.writeBufferVolume(wizardData.writeBufferVolume());
        });
        wizardData.reUsedStorageRouter.subscribe(function(sr) {
            if (sr === undefined && !wizardData.localHost() && wizardData.storageRoutersUsed().length > 0) {
                wizardData.hostAA('');
                wizardData.portAA(80);
                wizardData.clientIDAA('');
                wizardData.clientSecretAA('');
            }
            if (sr !== undefined && wizardData.vPool() !== undefined && wizardData.vPool().metadata().hasOwnProperty('backend_aa_' + sr.guid())) {
                var md = wizardData.vPool().metadata()['backend_aa_' + sr.guid()];
                if (md.hasOwnProperty('connection_info')) {
                    wizardData.hostAA(md.connection_info.host);
                    wizardData.portAA(md.connection_info.port);
                    wizardData.clientIDAA(md.connection_info.client_id);
                    wizardData.clientSecretAA(md.connection_info.client_secret);
                }
            }
        });

        // Computed
        wizardData.vPoolAdd = ko.computed(function() {
            return wizardData.vPool() === undefined;
        });
        wizardData.enhancedPresets = ko.computed(function(){
            if (wizardData.backend() === undefined){
                wizardData.preset(undefined);
                return []
            }
            var presets = parsePresets(wizardData.backend()),
                presetNames = [];
            $.each(wizardData.backend().presets, function(_, preset) {
                presetNames.push(preset.name);
            });
            if (wizardData.preset() === undefined) {
                wizardData.preset(presets[0]);
            } else if (!presetNames.contains(wizardData.preset().name)) {
                wizardData.preset(presets[0]);
            }
            return presets;
        });
        wizardData.enhancedPresetsAA = ko.computed(function(){
            if (wizardData.backendAA() === undefined){
                wizardData.presetAA(undefined);
                return []
            }
            var presets = parsePresets(wizardData.backendAA()),
                presetNames = [];
            $.each(wizardData.backendAA().presets, function(_, preset) {
                presetNames.push(preset.name);
            });
            if (wizardData.presetAA() === undefined) {
                wizardData.presetAA(presets[0]);
            } else if (!presetNames.contains(wizardData.presetAA().name)) {
                wizardData.presetAA(presets[0]);
            }
            return presets;
        });
        return wizardData;
    };
    return singleton();
});
