(function (global) {
  var tokenUrl = '/csrf-token';
  var tokenReady = fetch(tokenUrl, {
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json' }
  }).then(function (response) {
    return response.json();
  }).then(function (data) {
    global.__csrfToken = data && data.csrf_token;
    return global.__csrfToken;
  }).catch(function () {
    global.__csrfToken = null;
    return null;
  });

  global.ensureCsrfToken = function () {
    return tokenReady;
  };

  var origFetch = global.fetch.bind(global);
  global.fetch = function (input, init) {
    init = init || {};
    var method = String(init.method || 'GET').toUpperCase();
    if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') {
      return origFetch(input, init);
    }
    return tokenReady.then(function (token) {
      var headers = new Headers(init.headers || {});
      if (token && !headers.has('X-CSRF-Token') && !headers.has('X-CSRFToken')) {
        headers.set('X-CSRF-Token', token);
      }
      init.headers = headers;
      if (!init.credentials) {
        init.credentials = 'same-origin';
      }
      return origFetch(input, init);
    });
  };
})(window);
