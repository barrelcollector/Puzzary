(function () {
  const panel = document.getElementById("completion-celebration");

  const closeButton = document.getElementById("celebration-close");
  const soundToggle = document.getElementById("celebration-sound-toggle");
  const storageKey = "puzzaryCelebrationMuted";
  let hasShown = false;
  let audio;

  function soundMuted() {
    try { return window.localStorage.getItem(storageKey) === "true"; }
    catch (_) { return false; }
  }

  function updateSoundLabel() {
    if (!soundToggle) return;
    soundToggle.textContent = soundMuted() ? "Sound off" : "Sound on";
    soundToggle.setAttribute("aria-pressed", soundMuted() ? "true" : "false");
  }

  async function playNotes(notes, volume, spacing, duration) {
    if (soundMuted()) return;
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return;
    try {
      const context = audio ||= new AudioContext();
      await context.resume();
      if (soundMuted()) return;
      const now = context.currentTime;
      notes.forEach((frequency, index) => {
        const oscillator = context.createOscillator();
        const gain = context.createGain();
        oscillator.type = "sine";
        oscillator.frequency.value = frequency;
        const start = now + index * spacing;
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(volume, start + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + duration - 0.02);
        oscillator.connect(gain).connect(context.destination);
        oscillator.start(start);
        oscillator.stop(start + duration);
      });
    } catch (_error) {
      // Browser audio policies vary; the visual celebration is still enough.
    }
  }

  window.PuzzarySounds = {
    progress: () => playNotes([659.25, 783.99], 0.05, 0.09, 0.16),
    smallWin: () => playNotes([523.25, 659.25, 783.99], 0.06, 0.09, 0.18),
    complete: () => playNotes([523.25, 659.25, 783.99], 0.08, 0.1, 0.2),
    isMuted: soundMuted,
  };
  if (!panel) return;

  function showCelebration(options = {}) {
    if (hasShown && !options.force) return;
    hasShown = true;
    panel.classList.remove("is-hidden");
    panel.classList.add("show");
    window.PuzzarySounds.complete();
  }

  function hideCelebration() {
    panel.classList.add("is-hidden");
    panel.classList.remove("show");
  }

  window.PuzzaryCelebrate = {
    complete: showCelebration,
    hide: hideCelebration,
  };

  if (closeButton) closeButton.addEventListener("click", hideCelebration);
  if (soundToggle) {
    soundToggle.addEventListener("click", () => {
      window.localStorage.setItem(storageKey, soundMuted() ? "false" : "true");
      updateSoundLabel();
    });
  }

  updateSoundLabel();
  if (panel.dataset.showOnLoad === "true") {
    window.setTimeout(() => showCelebration({ force: true }), 120);
  }
})();
