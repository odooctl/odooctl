/* odooctl Web UI — vanilla JS SPA
 *
 * Talks only to the odooctl API (/projects, /operations, …).
 * No CLI imports, no docker/service access.
 * Token stored in localStorage; roles decoded from payload for client-side
 * hide/disable only — the server always re-checks RBAC.
 */
(function () {
    'use strict';

    // -------------------------------------------------------------------------
    // State
    // -------------------------------------------------------------------------
    var state = {
        token: localStorage.getItem('odooctl_token') || '',
        apiBase: localStorage.getItem('odooctl_api_base') || window.location.origin,
    };

    // -------------------------------------------------------------------------
    // API client
    // -------------------------------------------------------------------------
    function apiFetch(path, options) {
        options = options || {};
        var headers = Object.assign(
            { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + state.token },
            options.headers || {}
        );
        return fetch(state.apiBase + path, Object.assign({}, options, { headers: headers }))
            .then(function (resp) {
                if (!resp.ok) {
                    return resp.json().catch(function () { return { detail: resp.statusText }; })
                        .then(function (body) {
                            var err = new Error(body.detail || resp.statusText);
                            err.status = resp.status;
                            throw err;
                        });
                }
                return resp.json();
            });
    }

    // -------------------------------------------------------------------------
    // Token / RBAC helpers (client-side display only — server re-checks)
    // -------------------------------------------------------------------------
    function decodePayload() {
        if (!state.token) return null;
        try {
            var parts = state.token.split('.');
            if (parts.length !== 3) return null;
            var p = parts[1];
            var pad = '='.repeat((4 - (p.length % 4)) % 4);
            return JSON.parse(atob(p.replace(/-/g, '+').replace(/_/g, '/') + pad));
        } catch (e) { return null; }
    }

    var RANK = { viewer: 0, operator: 1, admin: 2, owner: 3 };

    function getRoles() {
        var payload = decodePayload();
        return (payload && payload.roles) ? payload.roles : ['viewer'];
    }

    function hasMinRole(minRole) {
        var roles = getRoles();
        var min = RANK[minRole] || 0;
        for (var i = 0; i < roles.length; i++) {
            if ((RANK[roles[i]] || 0) >= min) return true;
        }
        return false;
    }

    function isOperator() { return hasMinRole('operator'); }
    function isAdmin()    { return hasMinRole('admin'); }

    // -------------------------------------------------------------------------
    // HTML helpers
    // -------------------------------------------------------------------------
    function esc(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function formatTime(ts) {
        if (!ts) return '—';
        try {
            // ts may be ISO string or unix seconds
            var d = isNaN(Number(ts)) ? new Date(ts) : new Date(Number(ts) * 1000);
            return d.toLocaleString();
        } catch (e) { return String(ts); }
    }

    function renderHeader(title) {
        var payload = decodePayload();
        var sub = (payload && payload.sub) ? esc(payload.sub) : '';
        var roles = esc(getRoles().join(', '));
        return '<header class="top-bar">' +
            '<a class="logo" href="#/"><span class="logo-mark"></span>odooctl</a>' +
            (title ? '<span>' + esc(title) + '</span>' : '') +
            '<span class="user-info">' + (sub ? sub + ' &middot; ' : '') + roles + '</span>' +
            '<button class="btn btn-sm" id="logout-btn">Sign out</button>' +
            '</header>' +
            '<div class="page-body">';
    }

    function closePageBody() { return '</div>'; }

    function renderAlert(msg, type) {
        return '<div class="alert ' + (type || 'error') + '">' + esc(msg) + '</div>';
    }

    function renderSpinner() {
        return '<span class="spinner"></span>Loading…';
    }

    function renderEmptyState(message, hint) {
        return '<div class="empty-state">' +
            '<p>' + esc(message) + '</p>' +
            (hint ? '<p class="empty-hint">Run: <code>' + esc(hint) + '</code></p>' : '') +
            '</div>';
    }

    // -------------------------------------------------------------------------
    // Router
    // -------------------------------------------------------------------------
    function route() {
        var hash = window.location.hash.slice(1) || '/';
        var el = document.getElementById('app');

        if (!state.token) {
            renderLogin(el);
            return;
        }

        var m;
        if (hash === '/' || hash === '/projects') {
            renderProjects(el);
        } else if ((m = hash.match(/^\/project\/([^/]+)\/env\/([^/]+)$/))) {
            renderEnvDetail(el, decodeURIComponent(m[1]), decodeURIComponent(m[2]));
        } else if ((m = hash.match(/^\/project\/([^/]+)$/))) {
            renderProject(el, decodeURIComponent(m[1]));
        } else {
            el.innerHTML = renderHeader('') + '<p class="alert error">Page not found.</p>' + closePageBody();
        }
    }

    window.addEventListener('hashchange', route);

    // -------------------------------------------------------------------------
    // Login page
    // -------------------------------------------------------------------------
    function renderLogin(el) {
        el.innerHTML = '<div class="login-wrap"><div class="login-box">' +
            '<h1><span class="logo-mark logo-mark-lg"></span>odooctl Dashboard</h1>' +
            '<form id="login-form">' +
            '<label>API Token<input type="password" id="token-input" placeholder="Paste your bearer token" required></label>' +
            '<label>API Base URL<input type="text" id="base-input" value="' + esc(state.apiBase) + '"></label>' +
            '<button type="submit" class="btn btn-primary">Sign in</button>' +
            '</form>' +
            '<p class="hint">Generate a token: <code>odooctl security token mint --role operator</code></p>' +
            '</div></div>';

        el.querySelector('#login-form').addEventListener('submit', function (e) {
            e.preventDefault();
            state.token = el.querySelector('#token-input').value.trim();
            state.apiBase = el.querySelector('#base-input').value.trim().replace(/\/$/, '');
            localStorage.setItem('odooctl_token', state.token);
            localStorage.setItem('odooctl_api_base', state.apiBase);
            window.location.hash = '#/';
            route();
        });
    }

    // -------------------------------------------------------------------------
    // Projects page
    // -------------------------------------------------------------------------
    function renderProjects(el) {
        el.innerHTML = renderHeader('Dashboard') + renderSpinner() + closePageBody();

        apiFetch('/projects').then(function (data) {
            var projects = data.projects || [];
            var content = '<h2>Projects</h2>';
            if (!projects.length) {
                content += renderEmptyState(
                    'No projects registered.',
                    'odooctl project add <name> --path /srv/odoo/<name>'
                );
            } else {
                content += '<div class="project-grid">';
                projects.forEach(function (p) {
                    content += '<a class="project-card" href="#/project/' + encodeURIComponent(p) + '">' +
                        '<div class="project-name">' + esc(p) + '</div>' +
                        '</a>';
                });
                content += '</div>';
            }
            el.innerHTML = renderHeader('Dashboard') + content + closePageBody();
        }).catch(function (err) {
            el.innerHTML = renderHeader('Dashboard') + renderErrorAlert(err) + closePageBody();
        });
    }

    // -------------------------------------------------------------------------
    // Project detail page
    // -------------------------------------------------------------------------
    function renderProject(el, project) {
        el.innerHTML = renderHeader(project) + renderSpinner() + closePageBody();

        Promise.all([
            apiFetch('/projects/' + encodeURIComponent(project) + '/environments'),
            apiFetch('/projects/' + encodeURIComponent(project) + '/status'),
        ]).then(function (results) {
            var envsData = results[0];
            var statusData = results[1];
            var envs = envsData.environments || [];
            var statusByEnv = {};
            (statusData.environments || []).forEach(function (e) { statusByEnv[e.name] = e; });
            var recentOps = statusData.recent_operations || [];

            var content = '<nav class="breadcrumb"><a href="#/">Dashboard</a> &rsaquo; ' + esc(project) + '</nav>';
            content += '<h2>Environments</h2>';

            if (!envs.length) {
                content += '<p class="text-muted">No environments configured.</p>';
            } else {
                content += '<div class="env-grid">';
                envs.forEach(function (env) {
                    var st = statusByEnv[env.name] || {};
                    var tierClass = 'tier-' + (env.tier || 'development');
                    content += '<div class="env-card ' + esc(tierClass) + '">' +
                        '<div class="env-header">' +
                        '<span class="env-name">' + esc(env.name) + '</span>' +
                        '<span class="badge tier">' + esc(env.tier || 'dev') + '</span>' +
                        (env.protected ? '<span class="badge protected">🔒 protected</span>' : '') +
                        '</div>' +
                        '<div class="env-meta">' +
                        '<span>Branch: ' + esc(env.branch || '—') + '</span>' +
                        '<span>Backup: ' + esc(st.latest_backup || 'none') + '</span>' +
                        '<span>Deploy: ' + esc(st.last_deployment_status || '—') + '</span>' +
                        '</div>' +
                        '<div class="env-actions">' +
                        '<a class="btn btn-sm" href="#/project/' + encodeURIComponent(project) + '/env/' + encodeURIComponent(env.name) + '">Details</a>' +
                        (isOperator() ? '<button class="btn btn-sm" data-action="backup" data-project="' + esc(project) + '" data-env="' + esc(env.name) + '">Backup</button>' : '') +
                        '</div>' +
                        '</div>';
                });
                content += '</div>';
            }

            content += '<h2>Recent Operations</h2>';
            if (!recentOps.length) {
                content += renderEmptyState(
                    'No operations yet for this project.',
                    'odooctl backup <env>'
                );
            } else {
                content += buildOpsTable(recentOps);
            }

            el.innerHTML = renderHeader(project) + content + closePageBody();
            attachOpsTableEvents(el);
            el.querySelectorAll('[data-action="backup"]').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    var proj = btn.dataset.project;
                    var env = btn.dataset.env;
                    confirmAndRun(
                        'Run backup for environment <strong>' + esc(env) + '</strong>?',
                        'backup',
                        function () { enqueueOp(proj, { kind: 'backup', environment: env, params: {} }); }
                    );
                });
            });
        }).catch(function (err) {
            el.innerHTML = renderHeader(project) + renderErrorAlert(err) + closePageBody();
        });
    }

    // -------------------------------------------------------------------------
    // Environment detail page
    // -------------------------------------------------------------------------
    function renderEnvDetail(el, project, env) {
        el.innerHTML = renderHeader(project + ' › ' + env) + renderSpinner() + closePageBody();

        Promise.all([
            apiFetch('/projects/' + encodeURIComponent(project) + '/environments'),
            apiFetch('/projects/' + encodeURIComponent(project) + '/status'),
            apiFetch('/projects/' + encodeURIComponent(project) + '/backups'),
        ]).then(function (results) {
            var envsData   = results[0];
            var statusData = results[1];
            var backupsData = results[2];

            var envs = envsData.environments || [];
            var envCfg = null;
            envs.forEach(function (e) { if (e.name === env) envCfg = e; });
            if (!envCfg) envCfg = { name: env };

            var statusEnv = null;
            (statusData.environments || []).forEach(function (e) { if (e.name === env) statusEnv = e; });
            if (!statusEnv) statusEnv = {};

            var otherEnvs = envs.map(function (e) { return e.name; }).filter(function (n) { return n !== env; });
            var envOps = (statusData.recent_operations || []).filter(function (op) { return op.environment === env; });
            var envBackups = (backupsData.backups || []).filter(function (b) { return b.environment === env; });

            var isProtected = !!envCfg.protected;
            var canWrite = isOperator() && (!isProtected || isAdmin());
            // M15: operator-only principals must not launch migration rehearsal
            // against a protected source env (server enforces; this is UX).
            var migrateBlocked = isProtected && isOperator() && !isAdmin();

            var crumb = '<nav class="breadcrumb">' +
                '<a href="#/">Dashboard</a> &rsaquo; ' +
                '<a href="#/project/' + encodeURIComponent(project) + '">' + esc(project) + '</a> &rsaquo; ' +
                esc(env) +
                (isProtected ? ' <span class="badge protected">🔒 protected</span>' : '') +
                '</nav>';

            var tabs = '<div class="tabs">' +
                '<button class="tab active" data-tab="overview">Overview</button>' +
                '<button class="tab" data-tab="doctor">Doctor</button>' +
                '<button class="tab" data-tab="operations">Operations</button>' +
                '<button class="tab" data-tab="backups">Backups</button>' +
                '<button class="tab" data-tab="restore-points">Restore Points</button>' +
                (isOperator() ? '<button class="tab" data-tab="clone">Clone</button>' : '') +
                (isAdmin() ? '<button class="tab" data-tab="promote">Promote</button>' : '') +
                (isOperator()
                    ? '<button class="tab" data-tab="migrate"' +
                        (migrateBlocked
                            ? ' disabled title="Protected environment: migration rehearsal requires the admin role (you have operator). Server-side RBAC enforces this."'
                            : '') +
                        '>Migrate</button>'
                    : '') +
                '</div>' +
                '<div class="tab-content" id="tab-content">' +
                buildOverviewTab(envCfg, statusEnv) +
                '</div>';

            el.innerHTML = renderHeader(project + ' › ' + env) + crumb + tabs + closePageBody();

            el.querySelectorAll('.tab').forEach(function (tab) {
                tab.addEventListener('click', function () {
                    el.querySelectorAll('.tab').forEach(function (t) { t.classList.remove('active'); });
                    tab.classList.add('active');
                    var content = document.getElementById('tab-content');
                    var tabName = tab.dataset.tab;
                    if (tabName === 'overview') {
                        content.innerHTML = buildOverviewTab(envCfg, statusEnv);
                    } else if (tabName === 'doctor') {
                        content.innerHTML = buildDoctorTab(envCfg, statusEnv);
                    } else if (tabName === 'operations') {
                        content.innerHTML = envOps.length ? buildOpsTable(envOps) : renderEmptyState('No operations for this environment.', 'odooctl backup <env>');
                        attachOpsTableEvents(content);
                    } else if (tabName === 'backups') {
                        content.innerHTML = buildBackupsTab(envBackups, canWrite);
                        attachBackupsTab(content, project, env);
                    } else if (tabName === 'restore-points') {
                        content.innerHTML = renderSpinner();
                        fetchRestorePoints(project, env, content, canWrite);
                    } else if (tabName === 'clone') {
                        content.innerHTML = buildCloneTab(otherEnvs, canWrite, isProtected);
                        attachCloneTab(content, project, env, isProtected);
                    } else if (tabName === 'promote') {
                        content.innerHTML = buildPromoteTab(otherEnvs, isProtected);
                        attachPromoteTab(content, project, env);
                    } else if (tabName === 'migrate') {
                        content.innerHTML = buildMigrateTab(canWrite, isProtected);
                        attachMigrateTab(content, project, env, isProtected);
                    }
                });
            });
        }).catch(function (err) {
            el.innerHTML = renderHeader(project + ' › ' + env) + renderErrorAlert(err) + closePageBody();
        });
    }

    // ---- Tab content builders ----

    function buildOverviewTab(envCfg, statusEnv) {
        return '<div class="overview-grid">' +
            '<div class="info-card"><h3>Configuration</h3>' +
            '<table class="info-table">' +
            '<tr><td>Branch</td><td>' + esc(envCfg.branch || '—') + '</td></tr>' +
            '<tr><td>Domain</td><td>' + esc(envCfg.domain || '—') + '</td></tr>' +
            '<tr><td>Tier</td><td>' + esc(envCfg.tier || '—') + '</td></tr>' +
            '<tr><td>Protected</td><td>' + (envCfg.protected ? 'Yes' : 'No') + '</td></tr>' +
            '</table></div>' +
            '<div class="info-card"><h3>Status</h3>' +
            '<table class="info-table">' +
            '<tr><td>Last Deploy</td><td>' + esc(statusEnv.last_deployment_status || '—') + '</td></tr>' +
            '<tr><td>Commit</td><td><code>' + esc(statusEnv.last_deployment_commit || '—') + '</code></td></tr>' +
            '<tr><td>Latest Backup</td><td>' + esc(statusEnv.latest_backup || 'none') + '</td></tr>' +
            '</table></div>' +
            '</div>';
    }

    function buildDoctorTab(envCfg, statusEnv) {
        var checks = [
            { name: 'Configuration loaded', ok: Boolean(envCfg.name || envCfg.branch || envCfg.domain), detail: 'Environment metadata is readable through the API.' },
            { name: 'Deployment status', ok: Boolean(statusEnv.last_deployment_status), detail: statusEnv.last_deployment_status || 'No deployment status has been recorded yet.' },
            { name: 'Backup status', ok: Boolean(statusEnv.latest_backup), detail: statusEnv.latest_backup || 'No backup manifest has been recorded yet.' },
            { name: 'Protected policy', ok: true, detail: envCfg.protected ? 'Protected environment: destructive actions require admin+.' : 'Non-protected environment: operator actions allowed by server RBAC.' }
        ];
        var rows = checks.map(function (check) {
            return '<tr>' +
                '<td><span class="badge status-' + (check.ok ? 'succeeded' : 'queued') + '">' + (check.ok ? 'OK' : 'Pending') + '</span></td>' +
                '<td>' + esc(check.name) + '</td>' +
                '<td>' + esc(check.detail) + '</td>' +
                '</tr>';
        }).join('');
        return '<div class="info-card"><h3>Doctor</h3>' +
            '<p class="text-muted">Read-only health summary derived from API status/config data. It does not run CLI doctor or privileged checks in the web tier.</p>' +
            '<table class="ops-table"><tr><th>State</th><th>Check</th><th>Detail</th></tr>' + rows + '</table></div>';
    }

    function buildOpsTable(ops) {
        var rows = ops.map(function (op) {
            return '<tr' + (op.status === 'running' ? ' class="op-running"' : '') + '>' +
                '<td><code>' + esc(op.op_id.slice(0, 8)) + '&hellip;</code></td>' +
                '<td>' + esc(op.kind) + '</td>' +
                '<td>' + esc(op.environment) + '</td>' +
                '<td><span class="badge status-' + esc(op.status) + '">' + esc(op.status) + '</span></td>' +
                '<td>' + esc(formatTime(op.created_at)) + '</td>' +
                '<td><button class="btn btn-sm" data-action="stream-op" data-op-id="' + esc(op.op_id) + '">Logs</button></td>' +
                '</tr>';
        }).join('');
        return '<table class="ops-table">' +
            '<tr><th>ID</th><th>Kind</th><th>Env</th><th>Status</th><th>Created</th><th></th></tr>' +
            rows + '</table>';
    }

    function attachOpsTableEvents(container) {
        container.querySelectorAll('[data-action="stream-op"]').forEach(function (btn) {
            btn.addEventListener('click', function () { showOpLogs(btn.dataset.opId); });
        });
    }

    function buildBackupsTab(backups, canWrite) {
        var content = '';
        if (canWrite) {
            content += '<div class="tab-actions"><button class="btn" id="run-backup-btn">Run Backup Now</button></div>';
        } else if (!isOperator()) {
            content += renderAlert('Operator role required to create backups.', 'info');
        } else if (!isAdmin()) {
            content += renderAlert('Admin role required to back up a protected environment.', 'warning');
        }
        if (!backups.length) {
            content += renderEmptyState(
                'No backups found for this environment.',
                'odooctl backup <env>'
            );
        } else {
            content += '<table class="ops-table">' +
                '<tr><th>Timestamp</th><th>Path</th><th>Size</th></tr>';
            backups.forEach(function (b) {
                content += '<tr>' +
                    '<td>' + esc(b.timestamp || '—') + '</td>' +
                    '<td><code>' + esc(b.path || '—') + '</code></td>' +
                    '<td>' + esc(b.size != null ? String(b.size) : '—') + '</td>' +
                    '</tr>';
            });
            content += '</table>';
        }
        return content;
    }

    function attachBackupsTab(container, project, env) {
        var btn = container.querySelector('#run-backup-btn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            confirmAndRun(
                'Run backup for environment <strong>' + esc(env) + '</strong>?',
                'backup',
                function () { enqueueOp(project, { kind: 'backup', environment: env, params: {} }); }
            );
        });
    }

    function buildCloneTab(otherEnvs, canWrite, isProtected) {
        if (!canWrite) {
            var msg = isProtected
                ? 'Admin role required to clone a protected environment.'
                : 'Operator role required to clone environments.';
            return renderAlert(msg, 'warning');
        }
        if (!otherEnvs.length) {
            return '<p class="text-muted">No target environments available. Add another environment to enable cloning.</p>';
        }
        var opts = otherEnvs.map(function (e) {
            return '<option value="' + esc(e) + '">' + esc(e) + '</option>';
        }).join('');
        return '<div class="op-form">' +
            '<h3>Clone this environment</h3>' +
            '<p>Copies data from this environment into the target. The target service is restarted with the cloned data.</p>' +
            '<label>Target environment:<select id="clone-target">' + opts + '</select></label>' +
            '<label><input type="checkbox" id="clone-sanitize" checked> Sanitize (mask sensitive data in target)</label>' +
            '<div class="tab-actions mt-2"><button class="btn btn-danger" id="clone-btn">Clone → Target</button></div>' +
            (isProtected ? renderAlert('⚠ This is a protected environment — cloning requires explicit confirmation.', 'warning') : '') +
            '</div>';
    }

    function attachCloneTab(container, project, sourceEnv, isProtected) {
        var btn = container.querySelector('#clone-btn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            var target = (container.querySelector('#clone-target') || {}).value;
            var sanitize = (container.querySelector('#clone-sanitize') || {}).checked !== false;
            if (!target) return;
            var keyword = isProtected ? sourceEnv : 'clone';
            var msg = 'Clone <strong>' + esc(sourceEnv) + '</strong> &rarr; <strong>' + esc(target) + '</strong>.<br>' +
                (isProtected ? '<span class="alert warning" style="display:inline-block;margin-top:.5rem">Protected environment: this will overwrite ' + esc(target) + '.</span>' :
                    'This will overwrite data in <strong>' + esc(target) + '</strong>.');
            confirmAndRun(msg, keyword, function () {
                // The operation environment is always the environment the
                // runner will mutate.  Clone's source belongs in params.
                enqueueOp(project, { kind: 'clone', environment: target, params: { source: sourceEnv, sanitize: sanitize } });
            });
        });
    }

    function buildPromoteTab(otherEnvs, isProtected) {
        if (!otherEnvs.length) {
            return '<p class="text-muted">No target environments available for promotion.</p>';
        }
        var opts = otherEnvs.map(function (e) {
            return '<option value="' + esc(e) + '">' + esc(e) + '</option>';
        }).join('');
        return '<div class="op-form">' +
            '<h3>Promote to target</h3>' +
            '<p>Fast-forward merges this environment\'s branch into the target and redeploys. A pre-promote backup is taken automatically.</p>' +
            '<label>Promote to:<select id="promote-target">' + opts + '</select></label>' +
            '<div class="tab-actions mt-2"><button class="btn btn-danger" id="promote-btn">Promote</button></div>' +
            renderAlert('⚠ Promote is a deploy operation. Ensure the source branch is ready before proceeding.', 'warning') +
            '</div>';
    }

    function attachPromoteTab(container, project, sourceEnv) {
        var btn = container.querySelector('#promote-btn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            var target = (container.querySelector('#promote-target') || {}).value;
            if (!target) return;
            var msg = 'Promote <strong>' + esc(sourceEnv) + '</strong> &rarr; <strong>' + esc(target) + '</strong>.<br>This will merge branches and redeploy <strong>' + esc(target) + '</strong>.';
            confirmAndRun(msg, 'promote', function () {
                enqueueOp(project, { kind: 'promote', environment: sourceEnv, params: { target: target } });
            });
        });
    }

    function buildMigrateTab(canWrite, isProtected) {
        if (!canWrite) {
            var msg = isProtected
                ? 'Admin role required to run migration rehearsal on a protected environment.'
                : 'Operator role required to run migration rehearsal.';
            return renderAlert(msg, 'warning');
        }
        return '<div class="op-form">' +
            '<h3>Migration Rehearsal</h3>' +
            '<p>Clones the environment into a throwaway database, upgrades to the target version, healthchecks, produces a report, and drops the throwaway database. Production data is never modified.</p>' +
            '<label>Target version (e.g. 18.0):<input type="text" id="migrate-to" placeholder="18.0" autocomplete="off"></label>' +
            '<label><input type="checkbox" id="migrate-openupgrade"> Use OpenUpgrade (OCA) for the upgrade command</label>' +
            '<label><input type="checkbox" id="migrate-keep"> Keep throwaway database after rehearsal (for debugging)</label>' +
            '<div class="tab-actions mt-2"><button class="btn btn-danger" id="migrate-rehearse-btn">Run Rehearsal</button></div>' +
            renderAlert('Rehearsal never modifies the source database or filestore. All changes apply only to a throwaway database that is dropped after the run.', 'info') +
            (isProtected ? renderAlert('⚠ Protected environment — rehearsal clones production data into a temporary database.', 'warning') : '') +
            '</div>';
    }

    function attachMigrateTab(container, project, env, isProtected) {
        var btn = container.querySelector('#migrate-rehearse-btn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            var toInput = container.querySelector('#migrate-to');
            var to = toInput ? toInput.value.trim() : '';
            if (!to) { showToast('Enter a target version (e.g. 18.0).', 'error'); return; }
            var useOpenupgrade = !!(container.querySelector('#migrate-openupgrade') || {}).checked;
            var keep = !!(container.querySelector('#migrate-keep') || {}).checked;
            var msg = 'Run upgrade rehearsal for <strong>' + esc(env) + '</strong> &rarr; <strong>' + esc(to) + '</strong>.<br>' +
                '<span class="text-muted">Throwaway database will be ' + (keep ? 'kept after the run.' : 'dropped after the run.') + '</span>';
            confirmAndRun(msg, 'rehearse', function () {
                enqueueOp(project, {
                    kind: 'migrate_rehearsal',
                    environment: env,
                    params: { to: to, openupgrade: useOpenupgrade, keep: keep }
                });
            });
        });
    }

    function fetchRestorePoints(project, env, container, canWrite) {
        apiFetch('/projects/' + encodeURIComponent(project) + '/restore-points?environment=' + encodeURIComponent(env))
            .then(function (data) {
                container.innerHTML = buildRestorePointsTab(data.restore_points || [], env, canWrite);
                attachRestorePointsTab(container, project, env);
            })
            .catch(function (err) {
                container.innerHTML = renderErrorAlert(err);
            });
    }

    function buildRestorePointsTab(points, env, canWrite) {
        var content = '<div class="info-card"><h3>Restore Points</h3>';
        content += '<p class="text-muted">Restore points are verified local backup snapshots. Integrity is checked against manifest checksums.</p>';
        if (canWrite) {
            content += '<div class="tab-actions">' +
                '<button class="btn" id="run-backup-rp-btn">Create New Backup</button>' +
                (isAdmin() ? ' <button class="btn btn-warning" id="dr-drill-btn">DR Drill</button>' : '') +
                '</div>';
        }
        if (!points.length) {
            content += '<p class="text-muted">No restore points found for this environment.</p>';
        } else {
            content += '<table class="ops-table">' +
                '<tr><th>Backup ID</th><th>Timestamp</th><th>Integrity</th></tr>';
            points.forEach(function (p) {
                var integrityClass = p.integrity === 'ok' ? 'succeeded' : (p.integrity === 'failed' ? 'failed' : 'queued');
                content += '<tr>' +
                    '<td><code>' + esc(p.backup_id) + '</code></td>' +
                    '<td>' + esc(p.timestamp || '—') + '</td>' +
                    '<td><span class="badge status-' + esc(integrityClass) + '">' + esc(p.integrity) + '</span></td>' +
                    '</tr>';
            });
            content += '</table>';
        }
        content += '</div>';
        return content;
    }

    function attachRestorePointsTab(container, project, env) {
        var backupBtn = container.querySelector('#run-backup-rp-btn');
        if (backupBtn) {
            backupBtn.addEventListener('click', function () {
                confirmAndRun(
                    'Run backup for environment <strong>' + esc(env) + '</strong>?',
                    'backup',
                    function () { enqueueOp(project, { kind: 'backup', environment: env, params: {} }); }
                );
            });
        }
        var drBtn = container.querySelector('#dr-drill-btn');
        if (drBtn) {
            drBtn.addEventListener('click', function () {
                confirmAndRun(
                    'Run DR drill for <strong>' + esc(env) + '</strong>?<br>' +
                    '<span class="text-muted">Restores the latest backup into a throwaway database, runs a healthcheck, then cleans up.</span>',
                    'drill',
                    function () { enqueueOp(project, { kind: 'dr_drill', environment: env, params: {} }); }
                );
            });
        }
    }

    // -------------------------------------------------------------------------
    // Enqueue operation
    // -------------------------------------------------------------------------
    function enqueueOp(project, body) {
        apiFetch('/projects/' + encodeURIComponent(project) + '/operations', {
            method: 'POST',
            body: JSON.stringify(body),
        }).then(function (result) {
            showToast('Operation ' + result.op_id.slice(0, 8) + '… queued.', 'success');
            showOpLogs(result.op_id);
        }).catch(function (err) {
            showToast('Error: ' + err.message, 'error');
        });
    }

    // -------------------------------------------------------------------------
    // Operation log streaming (fetch + ReadableStream; avoids EventSource auth limitation)
    // -------------------------------------------------------------------------
    function showOpLogs(opId) {
        var overlay = document.createElement('div');
        overlay.className = 'overlay';
        overlay.innerHTML =
            '<div class="log-viewer">' +
            '<div class="log-header">' +
            '<strong>Operation logs</strong>' +
            '<span class="log-op-id">' + esc(opId) + '</span>' +
            '<button class="btn btn-sm close-btn">Close</button>' +
            '</div>' +
            '<div class="log-body" id="log-lines"></div>' +
            '<div class="log-status" id="log-status"><span class="spinner"></span>Connecting…</div>' +
            '</div>';
        document.body.appendChild(overlay);

        overlay.querySelector('.close-btn').addEventListener('click', function () { overlay.remove(); });

        var logLines = overlay.querySelector('#log-lines');
        var logStatus = overlay.querySelector('#log-status');

        function appendLine(event) {
            var line = document.createElement('div');
            var type = event.type || 'log';
            line.className = 'log-line log-' + type;
            var ts = event.timestamp ? '[' + event.timestamp + '] ' : '';
            line.textContent = ts + (event.message || JSON.stringify(event));
            logLines.appendChild(line);
            logLines.scrollTop = logLines.scrollHeight;
        }

        function setStatus(text, cls) {
            logStatus.innerHTML = text;
            if (cls) logStatus.className = 'log-status badge status-' + cls;
        }

        streamEvents(opId, appendLine, setStatus);
    }

    function streamEvents(opId, onEvent, onDone) {
        fetch(state.apiBase + '/operations/' + encodeURIComponent(opId) + '/events', {
            headers: { 'Authorization': 'Bearer ' + state.token },
        }).then(function (resp) {
            if (!resp.ok) { onDone('Connection failed (' + resp.status + ')', 'failed'); return; }
            var reader = resp.body.getReader();
            var decoder = new TextDecoder();
            var buffer = '';

            function pump() {
                reader.read().then(function (chunk) {
                    if (chunk.done) {
                        // Stream closed — fetch final status
                        apiFetch('/operations/' + encodeURIComponent(opId))
                            .then(function (op) { onDone(op.status, op.status); })
                            .catch(function () { onDone('done', 'unknown'); });
                        return;
                    }
                    buffer += decoder.decode(chunk.value, { stream: true });
                    var lines = buffer.split('\n');
                    buffer = lines.pop() || '';
                    lines.forEach(function (line) {
                        if (line.indexOf('data: ') === 0) {
                            try { onEvent(JSON.parse(line.slice(6))); } catch (e) { /* ignore parse errors */ }
                        }
                    });
                    pump();
                }).catch(function () { onDone('stream error', 'failed'); });
            }
            pump();
        }).catch(function (err) {
            onDone('Connection error: ' + err.message, 'failed');
        });
    }

    // -------------------------------------------------------------------------
    // Typed confirmation dialog (required for destructive actions)
    // -------------------------------------------------------------------------
    function confirmAndRun(htmlMessage, keyword, callback) {
        var overlay = document.createElement('div');
        overlay.className = 'overlay';
        overlay.innerHTML =
            '<div class="confirm-dialog">' +
            '<h3>Confirm Action</h3>' +
            '<p class="dialog-msg">' + htmlMessage + '</p>' +
            '<p class="label">Type <strong>' + esc(keyword) + '</strong> to confirm:</p>' +
            '<input type="text" id="confirm-input" placeholder="' + esc(keyword) + '" autocomplete="off" spellcheck="false">' +
            '<div class="confirm-buttons">' +
            '<button id="confirm-ok" class="btn btn-danger" disabled>Confirm</button>' +
            '<button id="confirm-cancel" class="btn">Cancel</button>' +
            '</div>' +
            '</div>';
        document.body.appendChild(overlay);

        var input  = overlay.querySelector('#confirm-input');
        var okBtn  = overlay.querySelector('#confirm-ok');
        var cancel = overlay.querySelector('#confirm-cancel');

        input.focus();
        input.addEventListener('input', function () {
            okBtn.disabled = input.value !== keyword;
        });
        okBtn.addEventListener('click', function () {
            overlay.remove();
            callback();
        });
        cancel.addEventListener('click', function () { overlay.remove(); });
        // Close on backdrop click
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) overlay.remove();
        });
    }

    // -------------------------------------------------------------------------
    // Toast notifications
    // -------------------------------------------------------------------------
    function showToast(msg, type) {
        var t = document.createElement('div');
        t.className = 'toast ' + (type || 'info');
        t.textContent = msg;
        document.body.appendChild(t);
        setTimeout(function () { t.remove(); }, 4000);
    }

    // -------------------------------------------------------------------------
    // Error rendering
    // -------------------------------------------------------------------------
    function renderErrorAlert(err) {
        if (err && err.status === 401) {
            return '<div class="alert error">Unauthorized — your token may have expired or be invalid. ' +
                '<button class="btn btn-sm" id="logout-btn" style="margin-left:.5rem">Sign out</button></div>';
        }
        return renderAlert(err ? err.message : 'Unknown error', 'error');
    }

    // -------------------------------------------------------------------------
    // Global click delegation (logout, etc.)
    // -------------------------------------------------------------------------
    document.addEventListener('click', function (evt) {
        if (evt.target && evt.target.id === 'logout-btn') {
            state.token = '';
            localStorage.removeItem('odooctl_token');
            window.location.hash = '#/';
            route();
        }
    });

    // -------------------------------------------------------------------------
    // Boot
    // -------------------------------------------------------------------------
    route();

}());
