import { forwardRef } from "react";

import { cn } from "../../lib/cn";

type Variant = "primary" | "navy" | "ghost" | "outline" | "danger" | "subtle";
type Size = "sm" | "md" | "lg";

const VARIANTS: Record<Variant, string> = {
  // Azure fill with a soft coloured shadow — the one high-emphasis action.
  primary:
    "bg-azure-500 text-white shadow-azure hover:bg-azure-600 hover:shadow-[0_10px_28px_-8px_rgb(0_163_250_/_0.6)] active:translate-y-px",
  // Deep navy, for the marketing surfaces where azure sits on azure.
  navy:
    "bg-navy-900 text-white shadow-navy hover:bg-navy-800 active:translate-y-px",
  outline:
    "border border-line-strong bg-white text-ink-800 shadow-soft hover:border-azure-300 hover:bg-azure-50 hover:text-navy-800",
  ghost: "text-ink-500 hover:bg-canvas-2 hover:text-ink-900",
  subtle: "bg-canvas-2 text-ink-700 hover:bg-navy-100 hover:text-navy-800",
  danger:
    "border border-bad-100 bg-bad-50 text-bad-600 hover:border-bad-500/40 hover:bg-bad-100",
};

const SIZES: Record<Size, string> = {
  sm: "h-8 gap-1.5 px-3 text-xs",
  md: "h-10 gap-2 px-4 text-sm",
  lg: "h-12 gap-2.5 px-6 text-[15px]",
};

const BASE =
  "inline-flex select-none items-center justify-center rounded-full font-medium no-underline " +
  "transition-all duration-200 disabled:pointer-events-none disabled:opacity-45";

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = "subtle", size = "md", loading, disabled, children, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cn(BASE, VARIANTS[variant], SIZES[size], className)}
      {...rest}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
});

/** Anchor styled as a button, for real navigations such as a file download. */
export function ButtonLink({
  className,
  variant = "primary",
  size = "md",
  children,
  ...rest
}: React.AnchorHTMLAttributes<HTMLAnchorElement> & { variant?: Variant; size?: Size }) {
  return (
    <a className={cn(BASE, VARIANTS[variant], SIZES[size], className)} {...rest}>
      {children}
    </a>
  );
}

function Spinner() {
  return (
    <svg className="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Primary call-to-action with a sweeping highlight (React Bits "Shimmer
 * Button"). Reserved for the single action that matters most on a screen.
 */
export function ShimmerButton({
  className,
  children,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={cn(
        "group relative inline-flex h-12 items-center justify-center gap-2 overflow-hidden rounded-full",
        "bg-gradient-to-r from-navy-900 via-navy-800 to-azure-600 px-7 text-[15px] font-semibold text-white",
        "shadow-[0_10px_30px_-10px_rgb(6_34_84_/_0.6)] transition-all duration-200",
        "hover:shadow-[0_14px_38px_-10px_rgb(0_163_250_/_0.6)] active:translate-y-px",
        "disabled:pointer-events-none disabled:opacity-45",
        className,
      )}
      {...rest}
    >
      <span
        aria-hidden
        className="absolute inset-0 -translate-x-full bg-[linear-gradient(110deg,transparent,rgb(255_255_255_/_0.42),transparent)] transition-transform duration-[1100ms] group-hover:translate-x-full"
      />
      <span className="relative inline-flex items-center gap-2">{children}</span>
    </button>
  );
}

/**
 * Pill button with a slowly rotating conic-gradient rim (Aceternity "Moving
 * Border"). Used for the hero call-to-action, where one element should feel
 * alive without animating the whole page.
 */
export function MovingBorderButton({
  className,
  children,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      className={cn(
        "group relative inline-flex h-12 items-center justify-center overflow-hidden rounded-full p-[1.5px]",
        "transition-transform duration-200 active:translate-y-px disabled:pointer-events-none disabled:opacity-45",
        className,
      )}
      {...rest}
    >
      <span
        aria-hidden
        className="absolute inset-[-200%] animate-border-spin bg-[conic-gradient(from_0deg,transparent_0deg,var(--color-azure-400)_70deg,var(--color-navy-700)_140deg,transparent_210deg)]"
      />
      <span className="relative inline-flex h-full w-full items-center justify-center gap-2 rounded-full bg-white px-6 text-[15px] font-semibold text-navy-900 transition-colors group-hover:bg-navy-50">
        {children}
      </span>
    </button>
  );
}
