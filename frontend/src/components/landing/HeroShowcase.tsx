import { motion } from "motion/react";

import { cn } from "../../lib/cn";
import { IconCheck, IconFilm, IconPlay, IconScissors, IconSparkle } from "../ui/Icons";

/**
 * The hero's product visual: a stylised app window with two cards floating
 * beside it, echoing the reference layout.
 *
 * Everything here is CSS and SVG rather than a screenshot, so it stays crisp at
 * any density, weighs nothing, and never goes stale when the real UI changes.
 * It is decorative — screen readers skip it.
 */

/** Deterministic "thumbnails": each clip gets a distinct navy→azure wash. */
const CLIPS = [
  { hue: "from-navy-800 via-navy-600 to-azure-500", time: "0:04", label: "Shot 01" },
  { hue: "from-azure-700 via-azure-500 to-azure-300", time: "0:03", label: "Shot 02" },
  { hue: "from-navy-900 via-navy-700 to-azure-600", time: "0:05", label: "Shot 03" },
  { hue: "from-azure-600 via-navy-600 to-navy-800", time: "0:02", label: "Shot 04" },
  { hue: "from-navy-700 via-azure-600 to-azure-400", time: "0:04", label: "Shot 05" },
  { hue: "from-azure-500 via-azure-400 to-navy-500", time: "0:03", label: "Shot 06" },
];

export function HeroShowcase() {
  return (
    <div aria-hidden className="relative mx-auto w-full max-w-5xl">
      {/* ------------------------------------------------- App window */}
      <motion.div
        initial={{ opacity: 0, y: 40, scale: 0.97 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.9, ease: [0.22, 1, 0.36, 1], delay: 0.15 }}
        className="relative overflow-hidden rounded-hero border border-line bg-paper shadow-float"
      >
        {/* Window chrome */}
        <div className="flex items-center gap-3 border-b border-line bg-canvas/80 px-5 py-3.5">
          <div className="flex gap-1.5">
            <span className="size-3 rounded-full bg-[#FF5F57]" />
            <span className="size-3 rounded-full bg-[#FEBC2E]" />
            <span className="size-3 rounded-full bg-[#28C840]" />
          </div>
          <div className="mx-auto hidden items-center gap-2 rounded-full border border-line bg-paper px-3.5 py-1 text-[11px] font-medium text-ink-400 sm:flex">
            <span className="size-1.5 rounded-full bg-ok-500" />
            scene-clipper · review
          </div>
        </div>

        <div className="p-5 sm:p-7">
          {/* Job summary strip */}
          <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <span className="grid size-9 place-items-center rounded-xl bg-gradient-to-br from-navy-900 to-azure-600 text-white">
                <IconFilm style={{ height: 17, width: 17 }} />
              </span>
              <div>
                <div className="h-2.5 w-32 rounded-full bg-ink-200 sm:w-44" />
                <div className="mt-1.5 h-2 w-20 rounded-full bg-canvas-2" />
              </div>
            </div>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-ok-100 bg-ok-50 px-2.5 py-1 text-[11px] font-semibold text-ok-600">
              <IconCheck style={{ height: 11, width: 11 }} strokeWidth={3} />
              24 clips ready
            </span>
          </div>

          {/* Shot timeline: the safe regions inside one long source */}
          <div className="mb-6">
            <div className="relative h-9 overflow-hidden rounded-xl bg-canvas-2">
              {[
                { left: "2%", width: "13%" },
                { left: "19%", width: "9%" },
                { left: "32%", width: "16%" },
                { left: "52%", width: "11%" },
                { left: "67%", width: "14%" },
                { left: "85%", width: "10%" },
              ].map((band, index) => (
                <motion.span
                  key={band.left}
                  initial={{ scaleX: 0, opacity: 0 }}
                  animate={{ scaleX: 1, opacity: 1 }}
                  transition={{
                    duration: 0.55,
                    delay: 0.7 + index * 0.09,
                    ease: [0.22, 1, 0.36, 1],
                  }}
                  style={{ left: band.left, width: band.width, transformOrigin: "left" }}
                  className="absolute inset-y-1.5 rounded-lg bg-gradient-to-r from-navy-700 to-azure-500 shadow-[0_2px_8px_-2px_rgb(0_163_250_/_0.6)]"
                />
              ))}
              {/* Cut markers the clips must never cross */}
              {["17%", "30%", "50%", "65%", "83%"].map((left) => (
                <span
                  key={left}
                  style={{ left }}
                  className="absolute inset-y-0 w-px bg-bad-500/45"
                />
              ))}
            </div>
            <div className="mt-2 flex justify-between text-[10px] font-medium text-ink-300">
              <span>00:00</span>
              <span>source timeline · cuts marked in red</span>
              <span>42:18</span>
            </div>
          </div>

          {/* Clip grid */}
          <div className="grid grid-cols-3 gap-3 sm:gap-4">
            {CLIPS.map((clip, index) => (
              <motion.div
                key={clip.label}
                initial={{ opacity: 0, y: 14 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.5, delay: 0.95 + index * 0.07 }}
                className="group/clip overflow-hidden rounded-xl border border-line bg-paper shadow-soft"
              >
                <div className={cn("relative aspect-video bg-gradient-to-br", clip.hue)}>
                  <span className="absolute inset-0 bg-[radial-gradient(circle_at_30%_20%,rgb(255_255_255_/_0.28),transparent_55%)]" />
                  <span className="absolute right-1.5 top-1.5 rounded-md bg-black/45 px-1.5 py-0.5 text-[9px] font-semibold text-white backdrop-blur-sm">
                    {clip.time}
                  </span>
                  <span className="absolute inset-0 grid place-items-center">
                    <span className="grid size-7 place-items-center rounded-full bg-white/90 text-navy-900 shadow-sm">
                      <IconPlay style={{ height: 10, width: 10 }} className="ml-px" />
                    </span>
                  </span>
                </div>
                <div className="flex items-center gap-1.5 px-2 py-1.5">
                  <span className="size-3 flex-none rounded-[4px] bg-azure-500" />
                  <span className="h-1.5 flex-1 rounded-full bg-canvas-2" />
                </div>
              </motion.div>
            ))}
          </div>
        </div>
      </motion.div>

      {/* ------------------------------------------ Floating card, left */}
      <motion.div
        initial={{ opacity: 0, x: -28, y: 16 }}
        animate={{ opacity: 1, x: 0, y: 0 }}
        transition={{ duration: 0.8, delay: 0.55, ease: [0.22, 1, 0.36, 1] }}
        className="absolute -left-4 top-20 hidden w-56 lg:block xl:-left-16"
      >
        <div className="animate-float rounded-panel border border-line bg-paper p-5 shadow-float">
          <span className="grid size-10 place-items-center rounded-xl bg-gradient-to-br from-azure-400 to-navy-700 text-white">
            <IconScissors style={{ height: 18, width: 18 }} />
          </span>
          <p className="mt-3.5 text-[15px] font-semibold leading-snug text-ink-900">
            Never crosses a cut
          </p>
          <p className="mt-1.5 text-xs leading-relaxed text-ink-400">
            Every clip stays inside one continuous shot — no cut, fade, or dissolve in the middle.
          </p>
        </div>
      </motion.div>

      {/* ----------------------------------------- Floating card, right */}
      <motion.div
        initial={{ opacity: 0, x: 28, y: -16 }}
        animate={{ opacity: 1, x: 0, y: 0 }}
        transition={{ duration: 0.8, delay: 0.7, ease: [0.22, 1, 0.36, 1] }}
        className="absolute -right-4 bottom-24 hidden w-52 lg:block xl:-right-16"
      >
        <div
          className="animate-float-slow rounded-panel border border-line bg-paper p-4 shadow-float"
          style={{ animationDelay: "-3s" }}
        >
          <div className="flex items-center gap-2">
            <span className="grid size-8 place-items-center rounded-lg bg-navy-50 text-navy-700">
              <IconSparkle style={{ height: 15, width: 15 }} />
            </span>
            <p className="text-xs font-semibold text-ink-900">Ranked by relevance</p>
          </div>
          <div className="mt-3 space-y-2">
            {[
              { w: "92%", label: "Product hero" },
              { w: "74%", label: "Product visible" },
              { w: "38%", label: "Person in shot" },
            ].map((row, index) => (
              <div key={row.label}>
                <div className="mb-1 flex items-center justify-between text-[10px] text-ink-400">
                  <span>{row.label}</span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-canvas-2">
                  <motion.div
                    initial={{ width: 0 }}
                    animate={{ width: row.w }}
                    transition={{ duration: 0.9, delay: 1.2 + index * 0.12 }}
                    className={cn(
                      "h-full rounded-full",
                      index === 2
                        ? "bg-bad-500/70"
                        : "bg-gradient-to-r from-navy-700 to-azure-500",
                    )}
                  />
                </div>
              </div>
            ))}
          </div>
        </div>
      </motion.div>
    </div>
  );
}
