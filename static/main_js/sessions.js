function sessionsHome() {
  return (typeof homeURL !== 'undefined' && homeURL) ? homeURL : '/';
}

function redirectIfSessionGone(response) {
  if (!response) {
    return false;
  }
  if (response.status === 401) {
    try { localStorage.setItem('loggedStatus', '0'); } catch (e) {}
    window.location.href = sessionsHome();
    return true;
  }
  return false;
}

function renderSessions(snapshot) {
  if (!snapshot) {
    return;
  }
  var banner = document.getElementById('sessions_new_device');
  if (banner) {
    if (snapshot.new_device) {
      banner.style.display = 'block';
      banner.textContent = 'New sign-in from ' + _currentDeviceLabel(snapshot) +
        '. If this was not you, sign out other devices and change your password.';
    } else {
      banner.style.display = 'none';
    }
  }
  var policy = document.getElementById('sessions_policy');
  if (policy) {
    var idleMin = Math.round((snapshot.idle_seconds || 0) / 60);
    var absHrs = Math.round((snapshot.absolute_seconds || 0) / 3600);
    policy.textContent = 'Idle timeout ' + idleMin + ' min • absolute lifetime ' +
      absHrs + ' h • up to ' + snapshot.max_concurrent + ' devices.';
  }
  var list = document.getElementById('sessions_list');
  if (!list) {
    return;
  }
  list.innerHTML = '';
  var sessions = snapshot.sessions || [];
  if (!sessions.length) {
    var empty = document.createElement('p');
    empty.style.color = 'white';
    empty.textContent = 'No active devices.';
    list.appendChild(empty);
    return;
  }
  sessions.forEach(function (item) {
    var card = document.createElement('div');
    card.className = 'session-card' + (item.current ? ' current' : '');
    var title = document.createElement('h5');
    title.style.marginTop = '0';
    title.appendChild(document.createTextNode(item.device_label || 'Unknown device'));
    if (item.current) {
      var badge = document.createElement('span');
      badge.className = 'session-badge';
      badge.textContent = 'This device';
      title.appendChild(badge);
    }
    if (item.new_device) {
      var neu = document.createElement('span');
      neu.className = 'session-new';
      neu.textContent = 'New';
      title.appendChild(neu);
    }
    card.appendChild(title);
    var ip = document.createElement('p');
    ip.className = 'session-meta';
    ip.textContent = 'IP: ' + (item.ip || 'unknown');
    card.appendChild(ip);
    var seen = document.createElement('p');
    seen.className = 'session-meta';
    seen.textContent = 'Last active: ' + (item.last_seen || '') + ' • Signed in: ' + (item.created_at || '');
    card.appendChild(seen);
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-primary';
    btn.textContent = item.current ? 'Sign out this device' : 'Revoke';
    btn.setAttribute('data-sid', item.sid);
    btn.addEventListener('click', function () {
      revokeSession(item.sid, item.current);
    });
    card.appendChild(btn);
    list.appendChild(card);
  });
}

function _currentDeviceLabel(snapshot) {
  var sessions = snapshot.sessions || [];
  for (var i = 0; i < sessions.length; i++) {
    if (sessions[i].current) {
      return (sessions[i].device_label || 'a new device') + ' (' + (sessions[i].ip || 'unknown IP') + ')';
    }
  }
  return 'a new device';
}

function refreshSessions() {
  var payload = {};
  if (typeof userid !== 'undefined' && userid) {
    payload.userid = userid;
  }
  return fetch(sessionsHome() + 'listSessions', {
    method: 'post',
    body: JSON.stringify(payload),
    headers: { 'Content-type': 'application/json' }
  }).then(function (response) {
    if (redirectIfSessionGone(response)) {
      return null;
    }
    return response.json();
  }).then(function (data) {
    if (data) {
      renderSessions(data);
    }
  }).catch(function (error) {
    console.error(error);
  });
}

function revokeSession(sid, isCurrent) {
  var payload = { sid: sid };
  if (typeof userid !== 'undefined' && userid) {
    payload.userid = userid;
  }
  fetch(sessionsHome() + 'revokeSession', {
    method: 'post',
    body: JSON.stringify(payload),
    headers: { 'Content-type': 'application/json' }
  }).then(function (response) {
    if (redirectIfSessionGone(response)) {
      return null;
    }
    return response.json().then(function (data) {
      return { ok: response.ok, data: data };
    });
  }).then(function (result) {
    if (!result) {
      return;
    }
    if (result.data && result.data.current) {
      window.location.href = sessionsHome();
      return;
    }
    refreshSessions();
  }).catch(function (error) {
    console.error(error);
  });
}

function revokeOtherSessions() {
  var payload = {};
  if (typeof userid !== 'undefined' && userid) {
    payload.userid = userid;
  }
  fetch(sessionsHome() + 'revokeOtherSessions', {
    method: 'post',
    body: JSON.stringify(payload),
    headers: { 'Content-type': 'application/json' }
  }).then(function (response) {
    if (redirectIfSessionGone(response)) {
      return null;
    }
    return response.json();
  }).then(function (data) {
    if (data) {
      refreshSessions();
    }
  }).catch(function (error) {
    console.error(error);
  });
}

function showSessionsPane() {
  var pane = document.getElementById('sessions_pane');
  if (!pane) {
    return;
  }
  if (window.jQuery) {
    window.jQuery(pane).show().siblings('div').hide();
  } else {
    pane.style.display = 'block';
  }
  refreshSessions();
}

function bootSessionsUi() {
  var menu = document.getElementById('sessions_menu');
  if (menu) {
    menu.addEventListener('click', function () {
      showSessionsPane();
      menu.style.backgroundColor = '#FF6600';
    });
  }
  var others = document.getElementById('revoke_other_sessions_btn');
  if (others) {
    others.addEventListener('click', revokeOtherSessions);
  }
  document.addEventListener('click', function (e) {
    var sessionsMenu = document.getElementById('sessions_menu');
    if (!sessionsMenu) {
      return;
    }
    var block = e.target.closest ? e.target.closest('.btn-primary.btn-block') : null;
    var account = e.target.closest ? e.target.closest('#account_details_btn') : null;
    if ((block && block.id !== 'sessions_menu') || account) {
      sessionsMenu.style.backgroundColor = 'maroon';
    }
  }, true);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootSessionsUi);
} else {
  bootSessionsUi();
}
