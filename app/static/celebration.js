(function () {
  const panel = document.getElementById("completion-celebration");
  if (!panel) return;

  const closeButton = document.getElementById("celebration-close");
  const soundToggle = document.getElementById("celebration-sound-toggle");
  const storageKey = "puzzaryCelebrationMuted";
  let hasShown = false;

  function soundMuted() {
    return window.localStorage.getItem(storageKey) === "true";
  }

  function updateSoundLabel() {
    if (!soundToggle) return;
    soundToggle.textContent = soundMuted() ? "Sound off" : "Sound on";
    soundToggle.setAttribute("aria-pressed", soundMuted() ? "true" : "false");
  }

  function playSuccessSound() {
    if (soundMuted()) return;
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return;
    try {
      const context = new AudioContext();
      const now = context.currentTime;
      const notes = [523.25, 659.25, 783.99];
      notes.forEach((frequency, index) => {
        const oscillator = context.createOscillator();
        const gain = context.createGain();
        oscillator.type = "sine";
        oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0.0001, now + index * 0.1);
        gain.gain.exponentialRampToValueAtTime(0.08, now + index * 0.1 + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + index * 0.1 + 0.18);
        oscillator.connect(gain).connect(context.destination);
        oscillator.start(now + index * 0.1);
        oscillator.stop(now + index * 0.1 + 0.2);
      });
      window.setTimeout(() => context.close(), 650);
    } catch (_error) {
      // Browser audio policies vary; the visual celebration is still enough.
    }
  }

  function showCelebration(options = {}) {
    if (hasShown && !options.force) return;
    hasShown = true;
    panel.classList.remove("is-hidden");
    panel.classList.add("show");
    playSuccessSound();
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
