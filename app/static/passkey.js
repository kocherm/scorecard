// Passkey ceremonies. The server does every check that matters; this file only
// translates between its JSON and the ArrayBuffers WebAuthn insists on.
//
// The one subtlety worth knowing: the browser API speaks ArrayBuffer, JSON does
// not, so every field the spec calls a BufferSource has to be decoded on the way
// in and re-encoded on the way out. base64url, not base64 - '+/' would come back
// mangled through a URL-safe transport.
(function () {
  "use strict";

  const supported = !!(window.PublicKeyCredential && navigator.credentials);

  function b64uToBuf(s) {
    const pad = "=".repeat((4 - (s.length % 4)) % 4);
    const bin = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }

  function bufToB64u(buf) {
    let bin = "";
    const bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body === undefined ? {} : body),
      credentials: "same-origin",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || "Something went wrong. Try again.");
    return data;
  }

  // A credential the server can verify: buffers out, everything else as-is.
  function serialize(cred) {
    const r = cred.response;
    const out = {
      id: cred.id,
      rawId: bufToB64u(cred.rawId),
      type: cred.type,
      clientExtensionResults: cred.getClientExtensionResults(),
      response: { clientDataJSON: bufToB64u(r.clientDataJSON) },
    };
    if (r.attestationObject) {
      out.response.attestationObject = bufToB64u(r.attestationObject);
      // Not a buffer, and the server stores it to hint at this key later.
      if (r.getTransports) out.response.transports = r.getTransports();
    } else {
      out.response.authenticatorData = bufToB64u(r.authenticatorData);
      out.response.signature = bufToB64u(r.signature);
      out.response.userHandle = r.userHandle
        ? new TextDecoder().decode(r.userHandle)
        : null;
    }
    return out;
  }

  // A cancelled prompt is a decision, not a failure - the user pressed Escape.
  // Reporting "NotAllowedError" at them would be noise.
  function message(err) {
    if (err && (err.name === "NotAllowedError" || err.name === "AbortError")) return null;
    if (err && err.name === "InvalidStateError")
      return "That device already has a passkey for this account.";
    return (err && err.message) || "Something went wrong. Try again.";
  }

  function show(el, text, kind) {
    if (!el) return;
    el.textContent = text || "";
    el.className = text ? "flash " + (kind || "err") : "";
  }

  // ------------------------------------------------------------- sign in
  const signInBtn = document.getElementById("passkey-signin");
  if (signInBtn) {
    if (!supported) {
      signInBtn.closest("[data-passkey-block]").hidden = true;
    } else {
      signInBtn.addEventListener("click", async function () {
        const out = document.getElementById("passkey-error");
        show(out, "");
        signInBtn.disabled = true;
        try {
          const opts = await post("/login/passkey/begin");
          opts.challenge = b64uToBuf(opts.challenge);
          (opts.allowCredentials || []).forEach(function (c) {
            c.id = b64uToBuf(c.id);
          });
          const cred = await navigator.credentials.get({ publicKey: opts });
          const done = await post("/login/passkey/finish", serialize(cred));
          window.location.assign(done.next || "/");
          return; // leave the button disabled through the navigation
        } catch (err) {
          show(out, message(err));
        }
        signInBtn.disabled = false;
      });
    }
  }

  // ------------------------------------------------------------- register
  const addBtn = document.getElementById("passkey-add");
  if (addBtn) {
    if (!supported) {
      addBtn.closest("[data-passkey-block]").hidden = true;
    } else {
      addBtn.addEventListener("click", async function () {
        const out = document.getElementById("passkey-msg");
        const nameEl = document.getElementById("passkey-name");
        show(out, "");
        addBtn.disabled = true;
        try {
          const opts = await post("/account/passkeys/begin");
          opts.challenge = b64uToBuf(opts.challenge);
          opts.user.id = b64uToBuf(opts.user.id);
          (opts.excludeCredentials || []).forEach(function (c) {
            c.id = b64uToBuf(c.id);
          });
          const cred = await navigator.credentials.create({ publicKey: opts });
          await post("/account/passkeys/finish", {
            credential: serialize(cred),
            name: (nameEl && nameEl.value) || "",
          });
          window.location.reload(); // the list is rendered server-side
          return;
        } catch (err) {
          show(out, message(err));
        }
        addBtn.disabled = false;
      });
    }
  }
})();
