import { motion } from "motion/react";
import { Link } from "react-router-dom";

import { HeroShowcase } from "../components/landing/HeroShowcase";
import { Button, MovingBorderButton, ShimmerButton } from "../components/ui/Button";
import {
  IconArrowRight,
  IconBolt,
  IconCheck,
  IconCpu,
  IconDownload,
  IconFrame,
  IconLayers,
  IconScissors,
  IconServer,
  IconShield,
  IconSparkle,
  IconTarget,
  IconWaveform,
} from "../components/ui/Icons";
import {
  Accordion,
  Badge,
  Reveal,
  SectionHeading,
} from "../components/ui/Primitives";
import { Aurora, GridPattern, Marquee, SpotlightCard } from "../components/ui/Spotlight";
import { MAX_CLIP_SECONDS, MIN_CLIP_SECONDS } from "../lib/clip";

/* ------------------------------------------------------------------ data */

const CAPABILITIES = [
  "Shot-boundary detection",
  "Frame-accurate trims",
  "Cut · fade · dissolve guards",
  "Product-only selection",
  "Gemini ranking with failover",
  "Resumable uploads",
  "H.264 + AAC export",
  "Batch MP4 download",
  "Local-first processing",
];

const FEATURES = [
  {
    icon: IconScissors,
    title: "Every clip is one continuous shot",
    body: "PySceneDetect finds each cut, fade, and dissolve, then a safety margin is trimmed from both ends. A clip can never contain a transition — that is a guarantee, not a heuristic.",
    span: "lg:col-span-2",
    accent: true,
  },
  {
    icon: IconTarget,
    title: "Product-only selection",
    body: "Name the product and only shots that clearly show it — with nobody in frame — are picked automatically.",
  },
  {
    icon: IconFrame,
    title: "Frame-accurate to the microsecond",
    body: "Bounds are computed on the source's exact rational timebase and quantised inward, so a trim never drifts by a frame.",
  },
  {
    icon: IconWaveform,
    title: "Audio and video start at zero",
    body: "Both streams are restamped so every exported clip opens in sync — verified at 0.000000 on every render.",
  },
  {
    icon: IconBolt,
    title: "Ranked, not shuffled",
    body: "Local measurements score sharpness, motion, and exposure. Gemini adds relevance on top — and if it is unavailable, ranking continues locally.",
  },
];

const STEPS = [
  {
    n: "01",
    title: "Upload one long video",
    body: "Drop a file of any length. The transfer is resumable and checksum-verified, so an interrupted upload continues from the last confirmed byte instead of starting over.",
    icon: IconLayers,
  },
  {
    n: "02",
    title: "Shots are detected locally",
    body: "FFmpeg and PySceneDetect map every transition in the source, then measure each candidate for sharpness, motion, and exposure — all on your own machine.",
    icon: IconCpu,
  },
  {
    n: "03",
    title: "Candidates are ranked",
    body: "Representative still frames go to Gemini for relevance scoring. Name a product and shots containing people are held back automatically.",
    icon: IconSparkle,
  },
  {
    n: "04",
    title: "Review, trim, export",
    body: `Adjust any clip inside its safe interval, reorder the set, then export H.264 MP4s at up to three resolutions — individually or as one download.`,
    icon: IconDownload,
  },
];

const GUARANTEES = [
  "Your full source video never leaves the machine",
  "Only representative still frames are sent for ranking",
  "API keys are stored AES-256-GCM encrypted, never returned",
  "Runs entirely self-hosted behind your own TLS",
];

const FAQ = [
  {
    q: "How long is each clip?",
    a: `Between ${MIN_CLIP_SECONDS} and ${MAX_CLIP_SECONDS} seconds. Within a long shot the best window in that range is chosen; shorter shots are used whole if they are long enough to qualify.`,
  },
  {
    q: "Can a clip contain a cut or a fade?",
    a: "No. Shot boundaries are detected before anything is ranked, and a safety margin is trimmed inward from both ends of every shot. The trim controls cannot be dragged outside that safe interval, and the server re-validates every submitted trim independently.",
  },
  {
    q: "Is my video uploaded to Google?",
    a: "No. Detection, measurement, trimming, and export all run locally. Only small representative still frames — and occasionally one short low-resolution excerpt to resolve ambiguous motion — are sent for ranking. The full source is never transmitted.",
  },
  {
    q: "What happens if my Gemini key runs out of quota?",
    a: "Up to five keys can be stored. When one is rejected or hits its limit the same request is re-sent on the next key automatically, and the failed key turns red in Settings. If every key is unavailable, ranking falls back to local measurements and no completed analysis is lost.",
  },
  {
    q: "Do I need Gemini at all?",
    a: "No. Turn it off per job, or store no key at all, and clips are ranked entirely on local measurements — sharpness, motion stability, exposure, and shot length. You still get a full, ordered result.",
  },
  {
    q: "What formats can I upload and export?",
    a: "Upload MP4, MOV, MKV, or WebM. Exports are H.264 video with AAC audio in a faststart MP4, at original size and optionally capped to 1080p or 720p. Output never exceeds the source dimensions — nothing is upscaled.",
  },
  {
    q: "Can I download several clips at once?",
    a: "Yes. Switch the export list into select mode, tick any number of clips, and take them as one ZIP with matching manifests — or save them as separate MP4 files.",
  },
  {
    q: "Where is my data stored?",
    a: "On your own server, in a directory you control. Nothing is sent to a third-party service other than the ranking frames described above, and deleting a job removes its source, previews, and exports from disk.",
  },
];

const STATS = [
  { value: `${MIN_CLIP_SECONDS}–${MAX_CLIP_SECONDS}s`, label: "Clip length range" },
  { value: "0 frames", label: "Drift on any trim" },
  { value: "5", label: "API keys with failover" },
  { value: "100%", label: "Local shot detection" },
];

/* ------------------------------------------------------------------ page */

export function LandingPage() {
  const primaryHref = "/jobs";
  const primaryLabel = "Open the app";

  return (
    <div className="overflow-clip">
      {/* ============================================================ Hero */}
      <section className="relative px-4 pb-20 pt-12 sm:px-6 sm:pb-28 sm:pt-16">
        <Aurora intensity="normal" />
        <GridPattern />

        <div className="relative mx-auto max-w-6xl">
          <motion.div
            initial={{ opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
            className="mx-auto max-w-3xl text-center"
          >
            <span className="inline-flex items-center gap-2 rounded-full border border-line bg-paper/80 px-3.5 py-1.5 text-xs font-medium text-ink-600 shadow-soft backdrop-blur">
              <span className="relative flex size-1.5">
                <span className="absolute inset-0 animate-pulse-ring rounded-full bg-azure-500" />
                <span className="relative size-1.5 rounded-full bg-azure-500" />
              </span>
              Self-hosted · your video never leaves your server
            </span>

            <h1 className="display-tight mt-6 text-[2.6rem] font-semibold text-ink-900 sm:text-6xl lg:text-[4.25rem]">
              Turn one long video into its{" "}
              <span className="relative inline-block whitespace-nowrap rounded-full bg-navy-900 px-5 pb-2 pt-1 text-white">
                best
              </span>{" "}
              short clips
            </h1>

            <p className="mx-auto mt-6 max-w-xl text-[15px] leading-relaxed text-ink-500 sm:text-base">
              Scene Clipper finds every continuous shot in your footage, ranks them, and exports
              frame-accurate {MIN_CLIP_SECONDS}–{MAX_CLIP_SECONDS} second clips — without ever
              crossing a cut.
            </p>

            <div className="mt-9 flex flex-wrap items-center justify-center gap-3">
              <Link to={primaryHref} className="no-underline">
                <ShimmerButton>
                  {primaryLabel}
                  <IconArrowRight style={{ height: 16, width: 16 }} />
                </ShimmerButton>
              </Link>
              <a href="#how" className="no-underline">
                <MovingBorderButton>See how it works</MovingBorderButton>
              </a>
            </div>

            <p className="mt-5 flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-xs text-ink-400">
              {["No watermarks", "No per-clip pricing", "Runs on your hardware"].map((item) => (
                <span key={item} className="inline-flex items-center gap-1.5">
                  <IconCheck style={{ height: 12, width: 12 }} className="text-ok-500" strokeWidth={3} />
                  {item}
                </span>
              ))}
            </p>
          </motion.div>

          <div className="mt-16 sm:mt-20">
            <HeroShowcase />
          </div>
        </div>
      </section>

      {/* ====================================================== Capability */}
      <section className="border-y border-line bg-paper/60 py-5">
        <Marquee duration="46s">
          {CAPABILITIES.map((item) => (
            <span
              key={item}
              className="inline-flex items-center gap-2 whitespace-nowrap rounded-full border border-line bg-paper px-4 py-2 text-[13px] font-medium text-ink-600 shadow-soft"
            >
              <span className="size-1.5 rounded-full bg-azure-500" />
              {item}
            </span>
          ))}
        </Marquee>
      </section>

      {/* ======================================================== Features */}
      <section id="features" className="px-4 py-20 sm:px-6 sm:py-28">
        <div className="mx-auto max-w-6xl">
          <Reveal>
            <SectionHeading
              eyebrow="Why it's different"
              title="Built on one rule that never bends"
              description="Most tools cut on a timer and hope. Scene Clipper detects the structure of your footage first, then works strictly inside it."
            />
          </Reveal>

          <div className="mt-14 grid gap-5 lg:grid-cols-3">
            {FEATURES.map((feature, index) => (
              <Reveal key={feature.title} delay={index * 0.06} className={feature.span}>
                <SpotlightCard
                  className={`h-full p-6 sm:p-7 ${
                    feature.accent ? "border-navy-100 bg-gradient-to-br from-navy-50 to-paper" : ""
                  }`}
                >
                  <span
                    className={`grid size-11 place-items-center rounded-xl ${
                      feature.accent
                        ? "bg-gradient-to-br from-navy-900 to-azure-600 text-white shadow-navy"
                        : "border border-line bg-canvas text-azure-600"
                    }`}
                  >
                    <feature.icon style={{ height: 20, width: 20 }} />
                  </span>
                  <h3 className="mt-5 text-lg font-semibold tracking-tight text-ink-900">
                    {feature.title}
                  </h3>
                  <p className="mt-2.5 text-sm leading-relaxed text-ink-500">{feature.body}</p>
                </SpotlightCard>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* ======================================================== The rule */}
      <section className="px-4 pb-20 sm:px-6 sm:pb-28">
        <div className="mx-auto max-w-6xl">
          <Reveal>
            <div className="relative overflow-hidden rounded-hero border border-navy-800 bg-navy-950 px-6 py-14 shadow-float sm:px-12 sm:py-16">
              <div
                aria-hidden
                className="pointer-events-none absolute inset-0 opacity-70"
                style={{
                  background:
                    "radial-gradient(40rem 24rem at 15% 0%, rgb(0 163 250 / 0.30), transparent 62%), radial-gradient(32rem 20rem at 90% 100%, rgb(28 80 166 / 0.42), transparent 60%)",
                }}
              />
              <div className="relative grid items-center gap-12 lg:grid-cols-2">
                <div>
                  <Badge tone="azure" className="border-white/15 bg-white/10 text-azure-200">
                    The continuity rule
                  </Badge>
                  <h2 className="mt-5 text-3xl font-semibold tracking-tight text-white sm:text-[2.5rem] sm:leading-tight">
                    A clip that crosses a cut is a broken clip
                  </h2>
                  <p className="mt-4 max-w-md text-[15px] leading-relaxed text-white/65">
                    Shot boundaries are found before anything is scored. A safety margin is trimmed
                    inward from each end, and the resulting safe interval is the only region any
                    clip may occupy — enforced in the editor and re-validated on the server.
                  </p>

                  <ul className="mt-8 space-y-3">
                    {[
                      "Adaptive + threshold detection catches hard cuts and slow dissolves alike",
                      "Motion is compensated so a pan is never mistaken for a transition",
                      "Trim handles are physically bounded by the safe interval",
                    ].map((line) => (
                      <li key={line} className="flex gap-3 text-sm text-white/75">
                        <span className="mt-0.5 grid size-5 flex-none place-items-center rounded-full bg-azure-500/20 text-azure-300">
                          <IconCheck style={{ height: 12, width: 12 }} strokeWidth={3} />
                        </span>
                        {line}
                      </li>
                    ))}
                  </ul>
                </div>

                {/* Annotated diagram of one shot's safe interval */}
                <div className="rounded-panel border border-white/10 bg-white/[0.06] p-6 backdrop-blur-sm">
                  <p className="mb-5 text-xs font-semibold uppercase tracking-wider text-white/45">
                    One detected shot
                  </p>
                  <div className="relative h-16 rounded-xl bg-white/10">
                    <span className="absolute inset-y-0 left-0 w-[12%] rounded-l-xl bg-bad-500/35" />
                    <span className="absolute inset-y-0 right-0 w-[12%] rounded-r-xl bg-bad-500/35" />
                    <span className="absolute inset-y-2 left-[12%] right-[12%] rounded-lg bg-gradient-to-r from-azure-500 to-azure-300 shadow-[0_0_24px_-4px_rgb(0_163_250_/_0.8)]" />
                    <span className="absolute inset-y-0 left-0 w-px bg-bad-500" />
                    <span className="absolute inset-y-0 right-0 w-px bg-bad-500" />
                  </div>
                  <div className="mt-3 flex justify-between text-[11px] text-white/45">
                    <span>cut</span>
                    <span className="font-semibold text-azure-200">safe interval</span>
                    <span>cut</span>
                  </div>
                  <p className="mt-5 text-xs leading-relaxed text-white/55">
                    The red margins absorb the transition itself plus any residual blend. Only the
                    blue region can ever appear in an exported clip.
                  </p>
                </div>
              </div>
            </div>
          </Reveal>
        </div>
      </section>

      {/* ==================================================== How it works */}
      <section id="how" className="scroll-mt-24 px-4 pb-20 sm:px-6 sm:pb-28">
        <div className="mx-auto max-w-6xl">
          <Reveal>
            <SectionHeading
              eyebrow="How it works"
              title="Four steps, one long video"
              description="From upload to a folder of finished MP4s, with a review pass in the middle where you stay in control."
            />
          </Reveal>

          <div className="mt-14 grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
            {STEPS.map((step, index) => (
              <Reveal key={step.n} delay={index * 0.08}>
                <div className="group relative h-full rounded-card border border-line bg-paper p-6 shadow-card transition-all duration-300 hover:-translate-y-1 hover:shadow-lift">
                  <div className="flex items-center justify-between">
                    <span className="grid size-11 place-items-center rounded-xl border border-line bg-canvas text-azure-600 transition-colors group-hover:border-azure-200 group-hover:bg-azure-50">
                      <step.icon style={{ height: 20, width: 20 }} />
                    </span>
                    <span className="font-display text-2xl font-semibold tracking-tight text-ink-200">
                      {step.n}
                    </span>
                  </div>
                  <h3 className="mt-5 text-base font-semibold tracking-tight text-ink-900">
                    {step.title}
                  </h3>
                  <p className="mt-2 text-sm leading-relaxed text-ink-500">{step.body}</p>
                </div>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* ========================================================== Stats */}
      <section className="border-y border-line bg-paper/70 px-4 py-14 sm:px-6">
        <div className="mx-auto grid max-w-5xl grid-cols-2 gap-8 lg:grid-cols-4">
          {STATS.map((stat, index) => (
            <Reveal key={stat.label} delay={index * 0.06} className="text-center">
              <p className="font-display text-3xl font-semibold tracking-tight text-brand-gradient sm:text-4xl">
                {stat.value}
              </p>
              <p className="mt-1.5 text-xs font-medium uppercase tracking-wider text-ink-400">
                {stat.label}
              </p>
            </Reveal>
          ))}
        </div>
      </section>

      {/* ======================================================== Privacy */}
      <section className="px-4 py-20 sm:px-6 sm:py-28">
        <div className="mx-auto grid max-w-6xl items-center gap-12 lg:grid-cols-2">
          <Reveal>
            <SectionHeading
              align="left"
              eyebrow="Privacy by architecture"
              title="Your footage stays on your hardware"
              description="This is not a SaaS you upload to. It is a container stack you run yourself, behind your own TLS, with one administrator account."
            />
            <ul className="mt-8 space-y-3.5">
              {GUARANTEES.map((line) => (
                <li key={line} className="flex gap-3 text-sm text-ink-600">
                  <span className="mt-0.5 grid size-5 flex-none place-items-center rounded-full bg-ok-50 text-ok-600">
                    <IconCheck style={{ height: 12, width: 12 }} strokeWidth={3} />
                  </span>
                  {line}
                </li>
              ))}
            </ul>
          </Reveal>

          <Reveal delay={0.1}>
            <div className="grid gap-4 sm:grid-cols-2">
              {[
                {
                  icon: IconServer,
                  title: "Self-hosted",
                  body: "Docker Compose: API, worker, Redis, and a TLS proxy. No external dependency at runtime.",
                },
                {
                  icon: IconShield,
                  title: "Encrypted at rest",
                  body: "Provider keys are sealed with AES-256-GCM and bound to their own record id.",
                },
                {
                  icon: IconCpu,
                  title: "Local analysis",
                  body: "Detection, measurement, trimming, and encoding all run on your machine.",
                },
                {
                  icon: IconFrame,
                  title: "Frames only",
                  body: "Ranking sees small contact sheets — never the source video.",
                },
              ].map((card, index) => (
                <div
                  key={card.title}
                  className={`rounded-card border border-line bg-paper p-5 shadow-card ${
                    index % 2 === 1 ? "sm:mt-8" : ""
                  }`}
                >
                  <span className="grid size-10 place-items-center rounded-xl bg-navy-50 text-navy-700">
                    <card.icon style={{ height: 18, width: 18 }} />
                  </span>
                  <p className="mt-4 text-sm font-semibold text-ink-900">{card.title}</p>
                  <p className="mt-1.5 text-xs leading-relaxed text-ink-500">{card.body}</p>
                </div>
              ))}
            </div>
          </Reveal>
        </div>
      </section>

      {/* ============================================================ FAQ */}
      <section id="faq" className="scroll-mt-24 px-4 pb-20 sm:px-6 sm:pb-28">
        <div className="mx-auto max-w-3xl">
          <Reveal>
            <SectionHeading
              eyebrow="FAQ"
              title="Questions, answered plainly"
              description="If something here is still unclear, the behaviour is documented in the project README."
            />
          </Reveal>
          <Reveal delay={0.08} className="mt-12">
            <Accordion items={FAQ} />
          </Reveal>
        </div>
      </section>

      {/* ============================================================ CTA */}
      <section className="px-4 pb-24 sm:px-6 sm:pb-32">
        <div className="mx-auto max-w-5xl">
          <Reveal>
            <div className="relative overflow-hidden rounded-hero border border-line bg-paper px-6 py-16 text-center shadow-float sm:px-12">
              <Aurora intensity="soft" />
              <div className="relative">
                <h2 className="display-tight mx-auto max-w-2xl text-3xl font-semibold text-ink-900 sm:text-[2.75rem]">
                  Ready to cut your first video?
                </h2>
                <p className="mx-auto mt-4 max-w-md text-[15px] text-ink-500">
                  Add your videos or links, then come back to a set of clips you can
                  review, trim, and export.
                </p>
                <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
                  <Link to={primaryHref} className="no-underline">
                    <ShimmerButton>
                      {primaryLabel}
                      <IconArrowRight style={{ height: 16, width: 16 }} />
                    </ShimmerButton>
                  </Link>
                  <a href="#features" className="no-underline">
                    <Button variant="outline" size="lg">
                      Explore the features
                    </Button>
                  </a>
                </div>
              </div>
            </div>
          </Reveal>
        </div>
      </section>
    </div>
  );
}
