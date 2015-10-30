// Copyright 2014 iNuron NV
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
/*global define  */
define(['jquery'], function ($) {
    'use strict';
    return {
        loadCss : function (fileName) {
            var cssTag = document.createElement('link');
            cssTag.setAttribute('rel', 'stylesheet');
            cssTag.setAttribute('type', 'text/css');
            cssTag.setAttribute('href', fileName);
            cssTag.setAttribute('class', '__dynamicCss');
            document.getElementsByTagName('head')[0].appendChild(cssTag);
        },
        removeModuleCss: function () {
            $('.__dynamicCss').remove();
        }
    };
});
