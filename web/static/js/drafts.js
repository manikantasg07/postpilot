// Drafts page: live LinkedIn preview, character counters, and busy buttons.
const SEE_MORE = 210;          // LinkedIn folds the post at roughly this many characters
const MAX_LENGTH = 3000;
const HOOK_LIMIT = 12;         // words in the first line that survive the fold

function paint(area) {
  const text = area.value;
  const preview = document.getElementById(area.dataset.mirror);
  const count = document.getElementById(area.dataset.count);
  const hook = document.getElementById(area.dataset.hook);

  if (preview) {
    preview.textContent = text;
    preview.classList.toggle("is-long", text.length > SEE_MORE);
  }

  if (count) {
    count.textContent = `${text.length} / ${MAX_LENGTH} characters`;
    count.className = text.length > MAX_LENGTH ? "bad" : "";
  }

  if (hook) {
    const words = text.trim().split(/\s+/).filter(Boolean);
    const first = (text.split("\n")[0] || "").trim();
    const hookWords = first.split(/\s+/).filter(Boolean).length;
    hook.textContent = `${words.length} words · hook ${hookWords} words`;
    hook.className = hookWords > HOOK_LIMIT ? "warn" : "";
  }
}

document.querySelectorAll("textarea[data-mirror]").forEach((area) => {
  paint(area);
  area.addEventListener("input", () => paint(area));
});

// "see more" fold, so you can check what people read before they click.
document.querySelectorAll(".li-text").forEach((block) => {
  block.addEventListener("click", () => block.classList.toggle("is-open"));
});
