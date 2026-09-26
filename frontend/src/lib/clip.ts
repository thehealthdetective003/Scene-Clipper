/**
 * Clip duration bounds.
 *
 * These mirror `MIN_CLIP_US` / `MAX_CLIP_US` in `backend/app/media/timebase.py`.
 * The client keeps handles inside them so illegal states are hard to reach, but
 * the server revalidates and frame-quantizes every submitted trim regardless.
 */

export const MIN_CLIP_US = 1_000_000;
export const MAX_CLIP_US = 5_000_000;

export const MIN_CLIP_SECONDS = MIN_CLIP_US / 1_000_000;
export const MAX_CLIP_SECONDS = MAX_CLIP_US / 1_000_000;

/** e.g. "1-5 second" for use in prose. */
export const CLIP_RANGE_LABEL = `${MIN_CLIP_SECONDS}–${MAX_CLIP_SECONDS}`;
