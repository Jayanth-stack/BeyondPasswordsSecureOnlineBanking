/* Reusable Idempotency-Key helper for money-moving POSTs.
 *
 * - One in-flight request per operation (double-click shares the same fetch).
 * - Same payload keeps the same key across network/5xx retries so the server
 *   can replay instead of debiting twice.
 * - 2xx / 4xx (except 409 in-progress) consume the key so a corrected form
 *   submits as a new operation.
 */
(function (global) {
  var inflight = {};
  var pendingKeys = {};

  function newKey() {
    if (global.crypto && typeof global.crypto.randomUUID === 'function') {
      return global.crypto.randomUUID();
    }
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function moneyPost(operation, url, body) {
    if (inflight[operation]) {
      return inflight[operation];
    }
    var payloadKey = operation + ':' + JSON.stringify(body);
    var key = pendingKeys[payloadKey] || newKey();
    pendingKeys[payloadKey] = key;

    var promise = fetch(url, {
      method: 'post',
      body: JSON.stringify(body),
      headers: {
        'Content-type': 'application/json',
        'Idempotency-Key': key
      }
    }).then(function (response) {
      if (response.status < 500 && response.status !== 409) {
        delete pendingKeys[payloadKey];
      }
      return response;
    }).finally(function () {
      delete inflight[operation];
    });

    inflight[operation] = promise;
    return promise;
  }

  global.Idempotency = {
    moneyPost: moneyPost,
    newKey: newKey
  };
})(window);
