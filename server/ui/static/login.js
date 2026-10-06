// D14: the login code travels in the URL fragment, which the browser never sends to the
// server (Uvicorn's access log records query strings). Read it, erase it from the address
// bar and the history, and trade it for the session cookie with one POST.
(function () {
  "use strict";
  var params = new URLSearchParams(window.location.hash.slice(1));
  var code = params.get("code");
  var next = params.get("next");
  window.history.replaceState(null, "", window.location.pathname);
  var status = document.getElementById("status");
  var again = "This sign-in link has expired or was already used. Run `loom ui` in a terminal to get a new one.";
  if (!code) {
    status.textContent = again;
    return;
  }
  // Only a path inside the UI: never another origin, never a protocol-relative URL.
  var target = next && next.indexOf("/ui/") === 0 ? next : "/ui/";
  fetch("/ui/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code: code }),
    credentials: "same-origin"
  }).then(function (response) {
    if (response.status === 204) {
      window.location.replace(target);
    } else if (response.status === 429) {
      status.textContent = "Too many failed sign-in attempts from this address. Wait a minute and run `loom ui` again.";
    } else {
      status.textContent = again;
    }
  }, function () {
    status.textContent = "Could not reach the Loom server. Check your connection and run `loom ui` again.";
  });
}());
