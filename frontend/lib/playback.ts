/**
 * Play/pause button state (issue #36 follow-up: local pause).
 *
 * Pausing is LOCAL only: the viewer stops audio on their end while the global radio keeps
 * advancing, and pressing Play re-syncs to the current live timestamp. So the toggle is
 * enabled whenever the station is airing a song, independent of whether this viewer is
 * currently listening; it only goes disabled when the station is idle (nothing to play).
 */

export interface PlayButtonInput {
  /** Is the global radio currently airing a song (vs. idle)? */
  radioPlaying: boolean;
  /** Is THIS viewer locally listening (vs. locally paused / not yet started)? */
  userPlaying: boolean;
}

export interface PlayButtonView {
  label: "Play" | "Pause";
  disabled: boolean;
}

export function resolvePlayButton({ radioPlaying, userPlaying }: PlayButtonInput): PlayButtonView {
  return {
    label: radioPlaying && userPlaying ? "Pause" : "Play",
    disabled: !radioPlaying,
  };
}
