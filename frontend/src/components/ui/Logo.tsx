import { cn } from "../../lib/cn";

/**
 * The brand mark.
 *
 * Served from `/logo.png` (Vite's public dir) rather than imported, so the same
 * file backs the favicon, the app chrome, and the landing page — one asset, one
 * cache entry. `logo-64.png` is a pre-scaled copy used at nav sizes so the
 * browser never downsamples a 512px image into a 36px box.
 */
export function LogoMark({
  size = 36,
  className,
  hero,
}: {
  size?: number;
  className?: string;
  /** Use the full-resolution file — for sizes above ~64px. */
  hero?: boolean;
}) {
  return (
    <span
      className={cn(
        "relative grid flex-none place-items-center overflow-hidden rounded-[28%]",
        "bg-white ring-1 ring-line shadow-soft",
        className,
      )}
      style={{ height: size, width: size }}
    >
      <img
        src={hero ? "/logo.png" : "/logo-64.png"}
        alt=""
        width={size}
        height={size}
        className="size-full object-contain"
        draggable={false}
      />
    </span>
  );
}

/** Mark plus wordmark, as used in the header and the footer. */
export function Logo({
  size = 36,
  className,
  tone = "ink",
  subtitle,
}: {
  size?: number;
  className?: string;
  tone?: "ink" | "invert";
  subtitle?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <LogoMark size={size} />
      <span className="flex flex-col leading-none">
        <span
          className={cn(
            "font-display text-[15px] font-semibold tracking-tight",
            tone === "invert" ? "text-white" : "text-ink-900",
          )}
        >
          Scene Clipper
        </span>
        {subtitle && (
          <span
            className={cn(
              "mt-1 text-[11px] font-medium tracking-wide",
              tone === "invert" ? "text-white/60" : "text-ink-400",
            )}
          >
            {subtitle}
          </span>
        )}
      </span>
    </span>
  );
}
