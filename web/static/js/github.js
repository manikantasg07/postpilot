// The device flow, from the page's side: ask for a code, then keep asking
// GitHub whether you have approved it yet.
const connect = document.getElementById("gh-connect");

async function post(url) {
  // A 500 returns HTML, not JSON, so parsing has to be allowed to fail. An
  // unhandled throw in here used to leave the page saying "waiting" for ever
  // while nothing was actually being waited on.
  const response = await fetch(url, { method: "POST" });
  try {
    return { ok: response.ok, data: await response.json() };
  } catch {
    return { ok: false, data: { error: `The app answered ${response.status}. ` +
                                       "Check the terminal running it for the reason." } };
  }
}

function say(message, bad = false) {
  const status = document.getElementById("gh-status");
  if (!status) return;
  status.textContent = message;
  status.style.color = bad ? "var(--rose)" : "var(--dim)";
}

function stop(timer, message) {
  clearInterval(timer);
  say(message, true);
  const again = document.getElementById("gh-retry");
  if (again) again.hidden = false;
}

function waitForApproval(seconds) {
  const started = Date.now();
  // GitHub's rule: when it answers slow_down, wait five seconds longer than
  // last time. Keep the old rate and it can answer slow_down for ever, which
  // looks exactly like you never approving.
  let gap = Math.max(seconds, 5) * 1000;
  let timer = null;

  async function tick() {
    // GitHub's codes last fifteen minutes. Polling past that is asking a
    // question that can no longer be answered yes.
    if (Date.now() - started > 15 * 60 * 1000) {
      stop(timer, "The code expired. Press Start over and try again.");
      return;
    }

    let result;
    try {
      result = await post("/setup/github/poll");
    } catch (err) {
      stop(timer, `Could not reach the app: ${err.message}`);
      return;
    }

    if (!result.ok) {                  // a real refusal: stop asking
      stop(timer, result.data.error || "GitHub refused the sign-in.");
      return;
    }
    if (result.data.status === "connected") {
      clearInterval(timer);
      say(`Connected as ${result.data.login}. Reloading…`);
      location.reload();
      return;
    }

    if (result.data.slow_down) {
      gap += 5000;
      clearInterval(timer);
      timer = setInterval(tick, gap);
      say(`GitHub asked us to slow down. Checking every ${gap / 1000}s…`);
    } else if (result.data.reason) {
      say("Waiting for you to approve on GitHub…");
    }
  }

  timer = setInterval(tick, gap);
}

if (connect) {
  connect.addEventListener("click", async () => {
    connect.disabled = true;
    connect.textContent = "Asking GitHub…";

    const { ok, data } = await post("/setup/github/start");
    if (!ok) {
      connect.disabled = false;
      connect.textContent = "Connect GitHub";
      say(data.error, true);
      return;
    }

    document.getElementById("gh-code").textContent = data.user_code;
    document.getElementById("gh-open").href = data.url;
    document.getElementById("gh-flow").hidden = false;
    connect.hidden = true;

    window.open(data.url, "_blank", "noopener");
    waitForApproval(data.interval);
  });
}

const retry = document.getElementById("gh-retry");
if (retry) retry.addEventListener("click", () => location.reload());

const copy = document.getElementById("gh-copy");
if (copy) {
  copy.addEventListener("click", async () => {
    await navigator.clipboard.writeText(document.getElementById("gh-code").textContent);
    copy.textContent = "✓";
    setTimeout(() => (copy.textContent = "⧉"), 1200);
  });
}
