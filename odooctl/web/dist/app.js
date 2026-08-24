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
        sessionUser: null,    // /auth/me result when signed in via session cookie
        sessionChecked: false, // whether the boot-time cookie probe has run
        runnerOnline: null,   // null = unknown, true/false once polled
        runnerTimer: null,
    };

    function isAuthed() { return !!(state.token || state.sessionUser); }

    // -------------------------------------------------------------------------
    // API client
    // -------------------------------------------------------------------------
    function apiFetch(path, options) {
        options = options || {};
        // Bearer token when one is set; otherwise the session cookie rides
        // along automatically (same-origin fetch).
        var base = { 'Content-Type': 'application/json' };
        if (state.token) base['Authorization'] = 'Bearer ' + state.token;
        var headers = Object.assign(base, options.headers || {});
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
        if (state.sessionUser && state.sessionUser.roles) return state.sessionUser.roles;
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

    function runnerPillHtml() {
        var cls, label, tip;
        if (state.runnerOnline === true) {
            cls = 'online'; label = 'Runner online';
            tip = 'A runner is processing operations.';
        } else if (state.runnerOnline === false) {
            cls = 'offline'; label = 'Runner offline';
            tip = 'No runner is processing operations — queued work will not run. Start one: odooctl runner';
        } else {
            cls = 'unknown'; label = 'Runner …';
            tip = 'Checking runner status…';
        }
        return '<span class="runner-dot"></span>' + esc(label) +
            '<span class="runner-tip">' + esc(tip) + '</span>';
    }

    function renderHeader(title) {
        var sub, exp = '';
        if (state.sessionUser) {
            sub = esc(state.sessionUser.display || state.sessionUser.id);
        } else {
            var payload = decodePayload();
            sub = (payload && payload.sub) ? esc(payload.sub) : '';
            if (payload && payload.exp) exp = ' &middot; expires ' + esc(formatTime(payload.exp));
        }
        var roles = esc(getRoles().join(', '));
        return '<header class="top-bar">' +
            '<a class="logo" href="#/"><span class="logo-mark"></span>odooctl</a>' +
            '<a class="top-link" href="#/access">Access</a>' +
            (title ? '<span>' + esc(title) + '</span>' : '') +
            '<span class="runner-pill runner-' + (state.runnerOnline === true ? 'online' : state.runnerOnline === false ? 'offline' : 'unknown') + '" id="runner-pill">' + runnerPillHtml() + '</span>' +
            '<span class="user-info">' + (sub ? sub + ' &middot; ' : '') + roles + exp + '</span>' +
            '<button class="btn btn-sm" id="refresh-btn" title="Refresh this page">&#8635;</button>' +
            '<button class="btn btn-sm" id="logout-btn">Sign out</button>' +
            '</header>' +
            '<div class="page-body">';
    }

    // Poll runner liveness and reflect it in the header pill. A missing/offline
    // runner is the reason enqueued operations sit "queued", so we surface it
    // prominently rather than letting the queue look broken.
    function updateRunnerPillDom() {
        var pill = document.getElementById('runner-pill');
        if (!pill) return;
        pill.className = 'runner-pill runner-' +
            (state.runnerOnline === true ? 'online' : state.runnerOnline === false ? 'offline' : 'unknown');
        pill.innerHTML = runnerPillHtml();
    }

    function pollRunner() {
        if (!isAuthed()) return;
        apiFetch('/runner/status').then(function (s) {
            state.runnerOnline = !!s.online;
            updateRunnerPillDom();
        }).catch(function () {
            // Leave the pill as-is on transient errors; auth errors are handled
            // by the page fetches themselves.
        });
    }

    function startRunnerPolling() {
        if (state.runnerTimer) return;
        pollRunner();
        state.runnerTimer = setInterval(pollRunner, 5000);
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

        if (!isAuthed()) {
            // A session cookie may already exist (page reload): probe once
            // before showing the login form.
            if (!state.sessionChecked) {
                state.sessionChecked = true;
                el.innerHTML = '<div class="login-wrap">' + renderSpinner() + '</div>';
                apiFetch('/auth/me').then(function (me) {
                    if (me && me.session) state.sessionUser = me;
                    route();
                }).catch(function () { route(); });
                return;
            }
            renderLogin(el);
            return;
        }

        startRunnerPolling();
        stopContainerPolling();

        var m;
        if (hash === '/' || hash === '/projects') {
            renderProjects(el);
        } else if (hash === '/access') {
            renderAccess(el);
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
            '<label>Email<input type="email" id="email-input" placeholder="you@example.com" autocomplete="username" required></label>' +
            '<label>Password<input type="password" id="password-input" autocomplete="current-password" required></label>' +
            '<button type="submit" class="btn btn-primary">Sign in</button>' +
            '<div id="login-error"></div>' +
            '</form>' +
            '<details class="login-alt"><summary>Sign in with an API token instead</summary>' +
            '<form id="token-form">' +
            '<label>API Token<input type="password" id="token-input" placeholder="Paste your bearer token"></label>' +
            '<label>API Base URL<input type="text" id="base-input" value="' + esc(state.apiBase) + '"></label>' +
            '<button type="submit" class="btn">Use token</button>' +
            '</form>' +
            '<p class="hint">Generate a token: <code>odooctl security token mint --action api --env \'*\' --project \'*\' --role operator</code></p>' +
            '</details>' +
            '<p class="hint">No account yet? Create one on the server: <code>odooctl user add you@example.com --role admin</code><br>' +
            'Operations run only while a runner is active: <code>odooctl runner</code></p>' +
            '</div></div>';

        el.querySelector('#login-form').addEventListener('submit', function (e) {
            e.preventDefault();
            var errBox = el.querySelector('#login-error');
            errBox.innerHTML = '';
            apiFetch('/auth/login', {
                method: 'POST',
                body: JSON.stringify({
                    email: el.querySelector('#email-input').value.trim(),
                    password: el.querySelector('#password-input').value,
                }),
            }).then(function () {
                return apiFetch('/auth/me');
            }).then(function (me) {
                state.sessionUser = me;
                window.location.hash = '#/';
                route();
            }).catch(function (err) {
                errBox.innerHTML = renderAlert(err.message, 'error');
            });
        });

        el.querySelector('#token-form').addEventListener('submit', function (e) {
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
    // Access page — RBAC matrix + admin token minting
    // -------------------------------------------------------------------------
    function renderAccess(el) {
        el.innerHTML = renderHeader('Access') + renderSpinner() + closePageBody();

        apiFetch('/rbac/matrix').then(function (data) {
            var matrix = data.matrix || {};
            var roleOrder = ['viewer', 'operator', 'admin', 'owner'];
            var roles = roleOrder.filter(function (r) { return matrix[r]; });
            var actions = roles.length ? Object.keys(matrix[roles[0]]) : [];
            var myRoles = getRoles();
            var destructive = data.destructive_on_protected || [];

            var content = '<nav class="breadcrumb"><a href="#/">Dashboard</a> &rsaquo; Access</nav>';
            content += '<h2>Your access</h2>';
            content += '<p>Signed in as <strong>' +
                esc(state.sessionUser ? (state.sessionUser.display || state.sessionUser.id) : 'token client') +
                '</strong> with role(s): <strong>' + esc(myRoles.join(', ')) + '</strong>. ' +
                (state.sessionUser
                    ? 'Roles come from your user account; the server re-checks every request.'
                    : 'Roles are carried inside the bearer token; the server re-checks every request.') + '</p>';

            content += '<h2>Role &rarr; action matrix</h2>';
            content += '<div class="matrix-wrap"><table class="ops-table rbac-matrix"><tr><th>Action</th>';
            roles.forEach(function (r) {
                var mine = myRoles.indexOf(r) !== -1;
                content += '<th' + (mine ? ' class="my-role" title="Your role"' : '') + '>' + esc(r) + (mine ? ' •' : '') + '</th>';
            });
            content += '</tr>';
            actions.forEach(function (a) {
                var prot = destructive.indexOf(a) !== -1;
                content += '<tr><td>' + esc(a) + (prot ? ' <span class="badge protected" title="On a protected environment this action requires admin or higher">🔒</span>' : '') + '</td>';
                roles.forEach(function (r) {
                    var ok = !!matrix[r][a];
                    content += '<td class="' + (ok ? 'cell-yes' : 'cell-no') + '">' + (ok ? '&#10003;' : '&mdash;') + '</td>';
                });
                content += '</tr>';
            });
            content += '</table></div>';
            content += '<p class="text-muted">🔒 = on a <em>protected</em> environment (production, or <code>tier: production</code>) ' +
                'this action additionally requires <strong>admin</strong> or higher, regardless of the base matrix. ' +
                'Restarting shared containers counts as protected whenever any environment in the project is protected.</p>';

            content += '<h2>Issue access tokens</h2>';
            if (isAdmin()) {
                content += '<div class="op-form" id="mint-form">' +
                    '<p>Mint a scoped bearer token for a teammate. The minted role cannot exceed your own, ' +
                    'TTL is capped at 7 days, and the token is shown once — it is never stored server-side.</p>' +
                    '<label>Role:<select id="mint-role">' +
                        '<option value="viewer">viewer — read-only</option>' +
                        '<option value="operator">operator — backup/clone/restore on non-protected envs</option>' +
                        '<option value="admin">admin — full control incl. protected envs</option>' +
                    '</select></label>' +
                    '<label>Valid for:<select id="mint-ttl">' +
                        '<option value="3600">1 hour</option>' +
                        '<option value="86400" selected>24 hours</option>' +
                        '<option value="604800">7 days</option>' +
                    '</select></label>' +
                    '<label>Project scope:<input type="text" id="mint-project" value="*" autocomplete="off"></label>' +
                    '<label>Subject (who is this for):<input type="text" id="mint-subject" placeholder="e.g. alice" autocomplete="off"></label>' +
                    '<div class="tab-actions mt-2"><button class="btn btn-primary" id="mint-btn">Mint token</button></div>' +
                    '<div id="mint-result"></div>' +
                    '</div>';
            } else {
                content += renderAlert('Admin role required to mint tokens. Ask an admin, or mint from the server shell: odooctl security token mint --action api --env \'*\' --project \'*\' --role <role>', 'info');
            }

            content += '<h2>User accounts</h2>';
            if (isAdmin()) {
                content += '<div id="users-panel">' + renderSpinner() + '</div>';
            } else {
                content += renderAlert('Admin role required to manage user accounts.', 'info');
            }

            el.innerHTML = renderHeader('Access') + content + closePageBody();
            if (isAdmin()) loadUsersPanel(el);

            var mintBtn = el.querySelector('#mint-btn');
            if (mintBtn) {
                mintBtn.addEventListener('click', function () {
                    var payload = {
                        role: el.querySelector('#mint-role').value,
                        ttl_seconds: parseInt(el.querySelector('#mint-ttl').value, 10),
                        project: el.querySelector('#mint-project').value.trim() || '*',
                        subject: el.querySelector('#mint-subject').value.trim() || undefined,
                    };
                    apiFetch('/tokens', { method: 'POST', body: JSON.stringify(payload) }).then(function (res) {
                        var box = el.querySelector('#mint-result');
                        box.innerHTML = '<div class="token-box">' +
                            '<p><strong>' + esc(res.role) + '</strong> token for <code>' + esc(res.subject) + '</code> ' +
                            '(project ' + esc(res.project) + ', ' + esc(String(Math.round(res.ttl_seconds / 3600))) + 'h). Copy it now — it is shown once:</p>' +
                            '<textarea readonly rows="3" id="minted-token">' + esc(res.token) + '</textarea>' +
                            '<button class="btn btn-sm" id="copy-token-btn">Copy</button>' +
                            '</div>';
                        box.querySelector('#copy-token-btn').addEventListener('click', function () {
                            var ta = box.querySelector('#minted-token');
                            ta.select();
                            try { document.execCommand('copy'); showToast('Token copied.', 'success'); } catch (e) { /* manual copy */ }
                        });
                    }).catch(function (err) {
                        showToast('Mint failed: ' + err.message, 'error');
                    });
                });
            }
        }).catch(function (err) {
            el.innerHTML = renderHeader('Access') + renderErrorAlert(err) + closePageBody();
        });
    }

    // Users panel on the Access page: list, create, disable/enable, delete.
    // Server enforces the role ceiling and self-guards; UI just surfaces errors.
    function loadUsersPanel(el) {
        var panel = el.querySelector('#users-panel');
        if (!panel) return;
        apiFetch('/users').then(function (data) {
            var users = data.users || [];
            var html = '';
            if (!users.length) {
                html += '<p class="text-muted">No user accounts yet. Accounts let teammates sign in ' +
                    'with email + password instead of pasted tokens.</p>';
            } else {
                html += '<table class="ops-table"><tr><th>Email</th><th>Name</th><th>Roles</th><th>Status</th><th></th></tr>';
                users.forEach(function (u) {
                    html += '<tr>' +
                        '<td>' + esc(u.email) + '</td>' +
                        '<td>' + esc(u.name || '—') + '</td>' +
                        '<td>' + esc((u.roles || []).join(', ') || '—') + '</td>' +
                        '<td>' + (u.disabled ? '<span class="badge protected">disabled</span>' : 'active') + '</td>' +
                        '<td class="ta-right">' +
                        '<button class="btn btn-sm" data-user-action="' + (u.disabled ? 'enable' : 'disable') + '" data-user-id="' + esc(u.id) + '">' + (u.disabled ? 'Enable' : 'Disable') + '</button> ' +
                        '<button class="btn btn-sm" data-user-action="delete" data-user-id="' + esc(u.id) + '" data-user-email="' + esc(u.email) + '">Delete</button>' +
                        '</td></tr>';
                });
                html += '</table>';
            }
            html += '<div class="op-form mt-2" id="user-create-form">' +
                '<p>Create an account. The granted role cannot exceed your own.</p>' +
                '<label>Email:<input type="email" id="new-user-email" autocomplete="off"></label>' +
                '<label>Password:<input type="password" id="new-user-password" autocomplete="new-password" placeholder="min 8 characters"></label>' +
                '<label>Role:<select id="new-user-role">' +
                    '<option value="viewer">viewer</option>' +
                    '<option value="operator">operator</option>' +
                    '<option value="admin">admin</option>' +
                '</select></label>' +
                '<div class="tab-actions mt-2"><button class="btn btn-primary" id="create-user-btn">Create user</button></div>' +
                '</div>';
            panel.innerHTML = html;

            panel.querySelector('#create-user-btn').addEventListener('click', function () {
                apiFetch('/users', {
                    method: 'POST',
                    body: JSON.stringify({
                        email: panel.querySelector('#new-user-email').value.trim(),
                        password: panel.querySelector('#new-user-password').value,
                        roles: [panel.querySelector('#new-user-role').value],
                    }),
                }).then(function () {
                    showToast('User created.', 'success');
                    loadUsersPanel(el);
                }).catch(function (err) {
                    showToast('Create failed: ' + err.message, 'error');
                });
            });

            panel.querySelectorAll('[data-user-action]').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    var action = btn.getAttribute('data-user-action');
                    var uid = btn.getAttribute('data-user-id');
                    var req;
                    if (action === 'delete') {
                        if (!window.confirm('Delete user ' + btn.getAttribute('data-user-email') + '?')) return;
                        req = apiFetch('/users/' + encodeURIComponent(uid), { method: 'DELETE' });
                    } else {
                        req = apiFetch('/users/' + encodeURIComponent(uid), {
                            method: 'PATCH',
                            body: JSON.stringify({ disabled: action === 'disable' }),
                        });
                    }
                    req.then(function () { loadUsersPanel(el); })
                        .catch(function (err) { showToast('Failed: ' + err.message, 'error'); });
                });
            });
        }).catch(function (err) {
            panel.innerHTML = renderErrorAlert(err);
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
            apiFetch('/projects/' + encodeURIComponent(project)),
        ]).then(function (results) {
            var envsData = results[0];
            var statusData = results[1];
            var projectInfo = results[2] || {};
            var envs = envsData.environments || [];
            var statusByEnv = {};
            (statusData.environments || []).forEach(function (e) { statusByEnv[e.name] = e; });
            var recentOps = statusData.recent_operations || [];

            var content = '<nav class="breadcrumb"><a href="#/">Dashboard</a> &rsaquo; ' + esc(project) + '</nav>';
            if (projectInfo.owner) {
                content += '<p class="text-muted">Owner: <strong>' + esc(projectInfo.owner) + '</strong></p>';
            }
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
                        (env.owner ? '<span>Owner: ' + esc(env.owner) + '</span>' : '') +
                        '</div>' +
                        '<div class="env-actions">' +
                        '<a class="btn btn-sm" href="#/project/' + encodeURIComponent(project) + '/env/' + encodeURIComponent(env.name) + '">Details</a>' +
                        (isOperator() ? '<button class="btn btn-sm" data-action="backup" data-project="' + esc(project) + '" data-env="' + esc(env.name) + '">Backup</button>' : '') +
                        '</div>' +
                        '</div>';
                });
                content += '</div>';
            }

            // Shared compose stack: live status + logs/restart per service.
            var projectProtected = envs.some(function (e) { return !!e.protected; });
            var canRestart = isOperator() && (!projectProtected || isAdmin());
            var firstEnv = envs.length ? envs[0].name : 'production';
            content += '<h2>Containers</h2><div id="containers-panel">' + renderSpinner() + '</div>';

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
            loadContainersPanel(project, 'containers-panel', firstEnv, {
                canRestart: canRestart,
                restartBlocked: projectProtected && isOperator() && !isAdmin(),
            });
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

            var projectProtected = envs.some(function (e) { return !!e.protected; });
            var canRestartSvc = isOperator() && (!projectProtected || isAdmin());

            var tabs = '<div class="tabs">' +
                '<button class="tab active" data-tab="overview">Overview</button>' +
                '<button class="tab" data-tab="containers">Containers</button>' +
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
                    stopContainerPolling();
                    var content = document.getElementById('tab-content');
                    var tabName = tab.dataset.tab;
                    if (tabName === 'overview') {
                        content.innerHTML = buildOverviewTab(envCfg, statusEnv);
                    } else if (tabName === 'containers') {
                        content.innerHTML = '<div id="containers-panel-env">' + renderSpinner() + '</div>';
                        loadContainersPanel(project, 'containers-panel-env', env, {
                            canRestart: canRestartSvc,
                            restartBlocked: projectProtected && isOperator() && !isAdmin(),
                        });
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
        var queuedOffline = state.runnerOnline === false && ops.some(function (op) { return op.status === 'queued'; });
        var banner = queuedOffline ? runnerOfflineBanner() : '';
        var rows = ops.map(function (op) {
            var actions = '<button class="btn btn-sm" data-action="stream-op" data-op-id="' + esc(op.op_id) + '">Logs</button>';
            if (op.status === 'queued') {
                actions += ' <button class="btn btn-sm btn-danger" data-action="cancel-op" data-op-id="' + esc(op.op_id) + '">Cancel</button>';
            }
            return '<tr' + (op.status === 'running' ? ' class="op-running"' : '') + '>' +
                '<td><code>' + esc(op.op_id.slice(0, 8)) + '&hellip;</code></td>' +
                '<td>' + esc(op.kind) + '</td>' +
                '<td>' + esc(op.environment) + '</td>' +
                '<td><span class="badge status-' + esc(op.status) + '">' + esc(op.status) + '</span></td>' +
                '<td>' + esc(formatTime(op.created_at)) + '</td>' +
                '<td class="op-actions">' + actions + '</td>' +
                '</tr>';
        }).join('');
        return banner + '<table class="ops-table">' +
            '<tr><th>ID</th><th>Kind</th><th>Env</th><th>Status</th><th>Created</th><th></th></tr>' +
            rows + '</table>';
    }

    // Explains why queued operations are not progressing, with the exact fix.
    function runnerOfflineBanner() {
        return '<div class="alert warning runner-banner">' +
            'No runner is processing operations, so queued work will not run. ' +
            'Start one on the server: <code>odooctl runner</code>' +
            '</div>';
    }

    function attachOpsTableEvents(container) {
        container.querySelectorAll('[data-action="stream-op"]').forEach(function (btn) {
            btn.addEventListener('click', function () { showOpLogs(btn.dataset.opId); });
        });
        container.querySelectorAll('[data-action="cancel-op"]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var opId = btn.dataset.opId;
                confirmAndRun(
                    'Cancel queued operation <code>' + esc(opId.slice(0, 8)) + '…</code>?',
                    'cancel',
                    function () { cancelOp(opId); }
                );
            });
        });
    }

    function cancelOp(opId) {
        apiFetch('/operations/' + encodeURIComponent(opId) + '/cancel', { method: 'POST' })
            .then(function (res) {
                showToast('Operation ' + opId.slice(0, 8) + '… ' + (res.status || 'cancelled') + '.', 'success');
                route();
            })
            .catch(function (err) { showToast('Cancel failed: ' + err.message, 'error'); });
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
    // Containers panel (live status from the runner's snapshot; logs/restart
    // go through the queue like every other privileged action)
    // -------------------------------------------------------------------------
    function stopContainerPolling() {
        if (state.containersTimer) { clearTimeout(state.containersTimer); state.containersTimer = null; }
    }

    function stateBadgeClass(c) {
        var s = (c.state || '').toLowerCase();
        var h = (c.health || '').toLowerCase();
        if (h === 'unhealthy' || s === 'exited' || s === 'dead') return 'failed';
        if (s === 'running') return 'succeeded';
        if (s === 'restarting' || s === 'created' || s === 'paused') return 'queued';
        return 'queued';
    }

    function buildContainersPanel(snapshot, opts) {
        var content = '';
        if (!snapshot.available) {
            content += renderAlert(
                'No container status yet — a runner probes the stack every few seconds. Start one: odooctl runner',
                'warning'
            );
            return content;
        }
        if (snapshot.stale) {
            content += renderAlert(
                'Container status is stale (last probe ' + esc(String(Math.round(snapshot.age_seconds || 0))) + 's ago) — is the runner still running?',
                'warning'
            );
        }
        if (snapshot.error) {
            content += renderAlert('Last probe error: ' + esc(snapshot.error), 'error');
        }
        var containers = snapshot.containers || [];
        if (!containers.length) {
            content += renderEmptyState('No containers found for this project’s compose stack.', 'docker compose up -d');
            return content;
        }
        var rows = containers.map(function (c) {
            var actions = '<button class="btn btn-sm" data-action="svc-logs" data-service="' + esc(c.service) + '">Logs</button>';
            if (opts.canRestart) {
                actions += ' <button class="btn btn-sm btn-danger" data-action="svc-restart" data-service="' + esc(c.service) + '">Restart</button>';
            }
            return '<tr>' +
                '<td><strong>' + esc(c.service) + '</strong></td>' +
                '<td><span class="badge status-' + stateBadgeClass(c) + '">' + esc(c.state || '?') + (c.health ? ' (' + esc(c.health) + ')' : '') + '</span></td>' +
                '<td>' + esc(c.status || '—') + '</td>' +
                '<td><code>' + esc(c.image || '—') + '</code></td>' +
                '<td class="op-actions">' + actions + '</td>' +
                '</tr>';
        }).join('');
        content += '<table class="ops-table">' +
            '<tr><th>Service</th><th>State</th><th>Uptime</th><th>Image</th><th></th></tr>' + rows + '</table>' +
            '<p class="text-muted containers-note">One compose stack serves every environment of this project — ' +
            'restarting a service affects all of them.' +
            (opts.restartBlocked ? ' Restart requires the admin role because this project has a protected environment.' : '') +
            '</p>';
        return content;
    }

    function attachContainersPanel(container, project, envName) {
        container.querySelectorAll('[data-action="svc-logs"]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                enqueueOp(project, {
                    kind: 'service_logs',
                    environment: envName,
                    params: { service: btn.dataset.service, tail: 200 }
                });
            });
        });
        container.querySelectorAll('[data-action="svc-restart"]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var svc = btn.dataset.service;
                confirmAndRun(
                    'Restart service <strong>' + esc(svc) + '</strong>?<br>' +
                    '<span class="text-muted">The shared container serves every environment of this project.</span>',
                    'restart',
                    function () {
                        enqueueOp(project, { kind: 'service_restart', environment: envName, params: { service: svc } });
                    }
                );
            });
        });
    }

    function loadContainersPanel(project, panelId, envName, opts) {
        var panel = document.getElementById(panelId);
        if (!panel) return;
        apiFetch('/projects/' + encodeURIComponent(project) + '/containers').then(function (snapshot) {
            var el = document.getElementById(panelId);
            if (!el) return; // navigated away
            el.innerHTML = buildContainersPanel(snapshot, opts);
            attachContainersPanel(el, project, envName);
            stopContainerPolling();
            state.containersTimer = setTimeout(function () {
                loadContainersPanel(project, panelId, envName, opts);
            }, 10000);
        }).catch(function (err) {
            var el = document.getElementById(panelId);
            if (el) el.innerHTML = renderErrorAlert(err);
        });
    }

    // -------------------------------------------------------------------------
    // Enqueue operation
    // -------------------------------------------------------------------------
    function enqueueOp(project, body) {
        apiFetch('/projects/' + encodeURIComponent(project) + '/operations', {
            method: 'POST',
            body: JSON.stringify(body),
        }).then(function (result) {
            if (state.runnerOnline === false) {
                showToast('Operation ' + result.op_id.slice(0, 8) + '… queued, but no runner is running — start "odooctl runner".', 'warning');
            } else {
                showToast('Operation ' + result.op_id.slice(0, 8) + '… queued.', 'success');
            }
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

        if (state.runnerOnline === false) {
            var hint = document.createElement('div');
            hint.className = 'log-line log-warning';
            hint.textContent = 'No runner is running — this operation will stay queued until you start one: odooctl runner';
            logLines.appendChild(hint);
        }

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
            logStatus.innerHTML = esc(text);
            if (cls) logStatus.className = 'log-status badge status-' + cls;
            // A terminal-looking stream that ends still "queued" means nothing
            // consumed it — point the operator at the runner.
            if (String(text).toLowerCase() === 'queued') {
                logStatus.innerHTML = 'queued — no runner consumed this yet. Start one: <code>odooctl runner</code>';
            }
        }

        streamEvents(opId, appendLine, setStatus);
    }

    function streamEvents(opId, onEvent, onDone) {
        var headers = state.token ? { 'Authorization': 'Bearer ' + state.token } : {};
        fetch(state.apiBase + '/operations/' + encodeURIComponent(opId) + '/events', {
            headers: headers,
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
        var target = evt.target;
        // Allow clicks on the icon/inner span of a button to still resolve.
        var btn = target && target.closest ? target.closest('button') : target;
        var id = (btn && btn.id) || (target && target.id);
        if (id === 'logout-btn') {
            var finishLogout = function () {
                state.token = '';
                state.sessionUser = null;
                localStorage.removeItem('odooctl_token');
                if (state.runnerTimer) { clearInterval(state.runnerTimer); state.runnerTimer = null; }
                state.runnerOnline = null;
                window.location.hash = '#/';
                route();
            };
            if (state.sessionUser) {
                apiFetch('/auth/logout', { method: 'POST' }).then(finishLogout, finishLogout);
            } else {
                finishLogout();
            }
        } else if (id === 'refresh-btn') {
            pollRunner();
            route();
        }
    });

    // -------------------------------------------------------------------------
    // Boot
    // -------------------------------------------------------------------------
    route();

}());
