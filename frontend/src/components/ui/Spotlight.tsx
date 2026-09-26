import { useCallback, useRef, useState } from "react";

import { cn } from "../../lib/cn";

/**
 * Card with a cursor-following radial highlight (Aceternity "Card Spotlight").
 *
 * On a light canvas the glow is a *tint*, not a bloom: a dark bloom would read
 * as a smudge. The gradient is driven by two CSS custom properties, so tracking
 * the pointer costs a style write rather than a React re-render of the subtree.
 */
export function SpotlightCard({
  className,
  children,
  glowColor = "rgb(0 163 250 / 0.10)",
  as: Tag = "div",
  ...rest
}: {
  className?: string;
  children: React.ReactNode;
  glowColor?: string;
  as?: "div" | "article" | "section";
} & React.HTMLAttributes<HTMLDivElement>) {
  const ref = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(false);

  const onMove = useCallback((event: React.MouseEvent<HTMLDivElement>) => {
    const node = ref.current;
    if (!node) return;
    const rect = node.getBoundingClientRect();
    node.style.setProperty("--spot-x", `${event.clientX - rect.left}px`);
    node.style.setProperty("--spot-y", `${event.clientY - rect.top}px`);
  }, []);

  return (
    <Tag
      ref={ref as never}
      onMouseMove={onMove}
      onMouseEnter={() => setActive(true)}
      onMouseLeave={() => setActive(false)}
      className={cn(
        "group relative overflow-hidden rounded-card border border-line bg-paper shadow-card",
        "transition-all duration-300 hover:-translate-y-0.5 hover:border-azure-200 hover:shadow-lift",
        className,
      )}
      {...rest}
    >
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 transition-opacity duration-300"
        style={{
          opacity: active ? 1 : 0,
          background: `radial-gradient(20rem circle at var(--spot-x, 50%) var(--spot-y, 0px), ${glowColor}, transparent 68%)`,
        }}
      />
      <div className="relative">{children}</div>
    </Tag>
  );
}

/**
 * A panel wrapped in a slowly rotating conic-gradient rim (Aceternity "Moving
 * Border"). Draws the eye to exactly one surface per screen.
 */
export function GlowBorder({
  className,
  children,
  active = true,
}: {
  className?: string;
  children: React.ReactNode;
  active?: boolean;
}) {
  return (
    <div className={cn("relative rounded-panel p-px shadow-card", className)}>
      {active && (
        <span aria-hidden className="absolute inset-0 overflow-hidden rounded-panel">
          <span className="absolute -inset-[100%] animate-border-spin bg-[conic-gradient(from_0deg,transparent_0%,var(--color-azure-400)_18%,var(--color-navy-600)_30%,transparent_45%)]" />
        </span>
      )}
      <div className="relative h-full w-full rounded-[calc(var(--radius-panel)-1px)] bg-paper">
        {children}
      </div>
    </div>
  );
}

/**
 * Drifting colour fields behind hero and auth screens (Aceternity "Background
 * Beams", light variant). Purely decorative and pointer-transparent.
 */
export function Aurora({
  className,
  intensity = "normal",
}: {
  className?: string;
  intensity?: "soft" | "normal" | "strong";
}) {
  const opacity = { soft: "opacity-45", normal: "opacity-70", strong: "opacity-95" }[intensity];
  return (
    <div
      aria-hidden
      className={cn("pointer-events-none absolute inset-0 overflow-hidden", opacity, className)}
    >
      <div className="absolute -left-[10%] -top-[30%] size-[46rem] animate-aurora rounded-full bg-[radial-gradient(circle,rgb(0_163_250_/_0.28),transparent_66%)] blur-3xl" />
      <div
        className="absolute -right-[14%] top-[-18%] size-[40rem] animate-aurora rounded-full bg-[radial-gradient(circle,rgb(28_80_166_/_0.22),transparent_66%)] blur-3xl"
        style={{ animationDelay: "-7s" }}
      />
      <div
        className="absolute bottom-[-28%] left-[22%] size-[36rem] animate-aurora rounded-full bg-[radial-gradient(circle,rgb(117_208_255_/_0.26),transparent_68%)] blur-3xl"
        style={{ animationDelay: "-14s" }}
      />
    </div>
  );
}

/** Fine dotted grid, faded out at the edges by a radial mask. */
export function GridPattern({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn("pointer-events-none absolute inset-0 dot-grid", className)}
      style={{
        maskImage: "radial-gradient(ellipse 80% 60% at 50% 0%, black 10%, transparent 75%)",
        WebkitMaskImage: "radial-gradient(ellipse 80% 60% at 50% 0%, black 10%, transparent 75%)",
      }}
    />
  );
}

/**
 * Seamless horizontal ticker (React Bits "Marquee"). The children are rendered
 * twice and the track is translated by exactly -50%, so the loop has no seam.
 */
export function Marquee({
  children,
  className,
  duration = "38s",
}: {
  children: React.ReactNode;
  className?: string;
  duration?: string;
}) {
  return (
    <div
      className={cn("group relative overflow-hidden", className)}
      style={{
        maskImage: "linear-gradient(to right, transparent, black 8%, black 92%, transparent)",
        WebkitMaskImage:
          "linear-gradient(to right, transparent, black 8%, black 92%, transparent)",
      }}
    >
      <div
        className="flex w-max animate-marquee items-center gap-3 group-hover:[animation-play-state:paused]"
        style={{ animationDuration: duration }}
      >
        <div className="flex shrink-0 items-center gap-3">{children}</div>
        <div className="flex shrink-0 items-center gap-3" aria-hidden>
          {children}
        </div>
      </div>
    </div>
  );
}
