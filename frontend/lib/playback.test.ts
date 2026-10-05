import { describe, expect, it } from "vitest";

// Issue #36 follow-up: local pause / resume. The Play button is a single toggle whose
// label and disabled state are a pure function of (radioPlaying, userPlaying):
// - radioPlaying: is the global radio currently airing a song (vs. idle)?
// - userPlaying:  is THIS viewer locally listening (vs. locally paused / not yet started)?
// Pausing is local only; the station keeps advancing, so the button stays enabled while
// the radio plays and only goes disabled when the station is idle (nothing to play/pause).
// RED-phase: imported dynamically so a missing export fails only these tests.

describe("resolvePlayButton", () => {
  it("shows Pause and stays enabled while the viewer is locally listening to a live radio", async () => {
    const { resolvePlayButton } = await import("./playback");
    expect(resolvePlayButton({ radioPlaying: true, userPlaying: true })).toEqual({
      label: "Pause",
      disabled: false,
    });
  });

  it("shows Play and stays enabled when the radio is live but the viewer is locally paused", async () => {
    const { resolvePlayButton } = await import("./playback");
    expect(resolvePlayButton({ radioPlaying: true, userPlaying: false })).toEqual({
      label: "Play",
      disabled: false,
    });
  });

  it("is disabled and labelled Play when the station is idle, regardless of local intent", async () => {
    const { resolvePlayButton } = await import("./playback");
    expect(resolvePlayButton({ radioPlaying: false, userPlaying: false })).toEqual({
      label: "Play",
      disabled: true,
    });
    // Even if a prior local-play intent lingers, an idle station cannot be paused/played.
    expect(resolvePlayButton({ radioPlaying: false, userPlaying: true })).toEqual({
      label: "Play",
      disabled: true,
    });
  });
});
