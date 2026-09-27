import { AnimatePresence, motion } from "motion/react";
import { useId, useState } from "react";

import { cn } from "../../lib/cn";
import { IconChevronDown } from "./Icons";

/* ------------------------------------------------------------------ Badge */

type Tone = "brand" | "azure" | "emerald" | "amber" | "rose" | "neutral";

/** Kept as aliases so existing call sites keep reading naturally. */
const TONES: Record<Tone, string> = {
  brand: "border-navy-100 bg-navy-50 text-ink-800",
  azure: "border-azure-100 bg-azure-50 text-azure-800",
  emerald: "border-ok-100 bg-ok-50 text-ok-600",
  amber: "border-warn-100 bg-warn-50 text-warn-600",
  rose: "border-bad-100 bg-bad-50 text-bad-600",
  neutral: "border-line bg-canvas text-ink-500",
};

export function Badge({
  tone = "neutral",
  className,
  children,
  dot,
}: {
  tone?: Tone;
  className?: string;
  children: React.ReactNode;
  dot?: boolean;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-1 text-xs font-medium",
        TONES[tone],
        className,
      )}
    >
      {dot && <span className="size-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

/** Maps a job/export/upload state onto a tone. */
export function StatusBadge({ state, label }: { state: string; label: string }) {
  const tone: Tone =
    state === "complete" || state === "review-ready" || state === "ready"
      ? "emerald"
      : state === "failed" || state === "cancelled"
        ? "rose"
        : state === "uploaded"
          ? "neutral"
          : "azure";
  const live = !["complete", "review-ready", "failed", "cancelled", "ready", "uploaded"].includes(
    state,
  );
  return (
    <Badge tone={tone} className="relative">
      <span className="relative flex size-1.5">
        {live && (
          <span className="absolute inset-0 animate-pulse-ring rounded-full bg-current" />
        )}
        <span className="relative size-1.5 rounded-full bg-current" />
      </span>
      {label}
    </Badge>
  );
}

/* ------------------------------------------------------------------- Card */

export function Panel({ className, children, ...rest }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("rounded-panel border border-line bg-paper p-5 shadow-card sm:p-6", className)}
      {...rest}
    >
      {children}
    </div>
  );
}

export function PanelHeader({
  title,
  description,
  actions,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("mb-4 flex flex-wrap items-start justify-between gap-3", className)}>
      <div className="min-w-0">
        <h2 className="text-base font-semibold tracking-tight text-ink-900">{title}</h2>
        {description && <p className="mt-1 text-sm text-ink-500">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

/** Section heading used across the marketing page. */
export function SectionHeading({
  eyebrow,
  title,
  description,
  align = "center",
  className,
}: {
  eyebrow?: React.ReactNode;
  title: React.ReactNode;
  description?: React.ReactNode;
  align?: "center" | "left";
  className?: string;
}) {
  return (
    <div
      className={cn(
        "max-w-2xl",
        align === "center" ? "mx-auto text-center" : "text-left",
        className,
      )}
    >
      {eyebrow && (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-azure-100 bg-azure-50 px-3 py-1 text-xs font-semibold uppercase tracking-wider text-azure-700">
          {eyebrow}
        </span>
      )}
      <h2 className="mt-4 text-3xl font-semibold tracking-tight text-ink-900 sm:text-[2.6rem] sm:leading-[1.08]">
        {title}
      </h2>
      {description && (
        <p className={cn("mt-4 text-[15px] leading-relaxed text-ink-500", align === "center" && "mx-auto")}>
          {description}
        </p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ Input */

export function Field({
  label,
  hint,
  error,
  children,
  className,
}: {
  label: React.ReactNode;
  hint?: React.ReactNode;
  error?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <label className={cn("block", className)}>
      <span className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-ink-400">
        {label}
      </span>
      {children}
      {hint && !error && <span className="mt-1.5 block text-xs text-ink-400">{hint}</span>}
      {error && <span className="mt-1.5 block text-xs text-bad-600">{error}</span>}
    </label>
  );
}

export const inputStyles =
  "w-full rounded-xl border border-line-strong bg-paper px-3.5 py-2.5 text-sm text-ink-900 " +
  "placeholder:text-ink-300 shadow-[inset_0_1px_2px_rgb(6_34_84_/_0.04)] transition-all duration-200 " +
  "hover:border-ink-200 focus:border-azure-400 focus:outline-none focus:ring-4 focus:ring-azure-500/12";

export function Input({ className, ...rest }: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(inputStyles, className)} {...rest} />;
}

export function Textarea({ className, ...rest }: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(inputStyles, "resize-y", className)} {...rest} />;
}

export function Select({ className, ...rest }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(inputStyles, "cursor-pointer appearance-none pr-9", className)}
      style={{
        backgroundImage:
          "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='%237887a5'%3E%3Cpath d='M4.2 6.2a.75.75 0 0 1 1.06 0L8 8.94l2.74-2.74a.75.75 0 1 1 1.06 1.06l-3.27 3.27a.75.75 0 0 1-1.06 0L4.2 7.26a.75.75 0 0 1 0-1.06z'/%3E%3C/svg%3E\")",
        backgroundRepeat: "no-repeat",
        backgroundPosition: "right 0.65rem center",
        backgroundSize: "1rem",
      }}
      {...rest}
    />
  );
}

/* ----------------------------------------------------------------- Switch */

export function Switch({
  checked,
  onChange,
  disabled,
  label,
  description,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label: React.ReactNode;
  description?: React.ReactNode;
}) {
  const id = useId();
  return (
    <div className="flex items-start gap-3">
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cn(
          "relative mt-0.5 inline-flex h-6 w-11 flex-none items-center rounded-full transition-colors duration-300",
          "disabled:opacity-45",
          checked ? "bg-azure-500 shadow-azure" : "bg-ink-200",
        )}
      >
        <motion.span
          layout
          transition={{ type: "spring", stiffness: 520, damping: 32 }}
          className={cn(
            "rounded-full bg-white shadow-[0_1px_3px_rgb(6_34_84_/_0.3)]",
            checked ? "ml-auto mr-1" : "ml-1",
          )}
          style={{ height: 18, width: 18 }}
        />
      </button>
      <label htmlFor={id} className="cursor-pointer select-none text-sm text-ink-700">
        {label}
        {description && <span className="mt-0.5 block text-xs text-ink-400">{description}</span>}
      </label>
    </div>
  );
}

/* --------------------------------------------------------------- Checkbox */

export function Checkbox({
  checked,
  onChange,
  disabled,
  label,
  className,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label: React.ReactNode;
  className?: string;
}) {
  return (
    <label
      className={cn(
        "flex cursor-pointer select-none items-start gap-2.5 text-sm text-ink-700",
        disabled && "cursor-not-allowed opacity-50",
        className,
      )}
    >
      <span className="relative mt-0.5 flex size-[18px] flex-none items-center justify-center">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
          className="peer size-full cursor-pointer appearance-none rounded-md border border-line-strong bg-paper transition-colors checked:border-azure-500 checked:bg-azure-500 disabled:cursor-not-allowed"
        />
        <svg
          viewBox="0 0 16 16"
          aria-hidden
          className="pointer-events-none absolute size-3 scale-0 text-white transition-transform peer-checked:scale-100"
        >
          <path
            d="M3.5 8.2l3 3 6-6.4"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </span>
      <span>{label}</span>
    </label>
  );
}

/* --------------------------------------------------------------- Progress */

export function Progress({
  value,
  className,
  indeterminate,
}: {
  value?: number;
  className?: string;
  indeterminate?: boolean;
}) {
  const pct = Math.max(0, Math.min(100, value ?? 0));
  return (
    <div
      role="progressbar"
      aria-valuenow={indeterminate ? undefined : Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      className={cn("h-2 w-full overflow-hidden rounded-full bg-canvas-2", className)}
    >
      {indeterminate ? (
        <div className="h-full w-1/3 animate-marquee rounded-full bg-gradient-to-r from-transparent via-azure-500 to-transparent" />
      ) : (
        <motion.div
          className="h-full rounded-full bg-gradient-to-r from-navy-700 via-azure-600 to-azure-400"
          initial={false}
          animate={{ width: `${pct}%` }}
          transition={{ type: "spring", stiffness: 120, damping: 24 }}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------- Tabs */

export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  className,
  size = "md",
}: {
  options: ReadonlyArray<{ value: T; label: string }>;
  value: T;
  onChange: (next: T) => void;
  className?: string;
  size?: "sm" | "md";
}) {
  const groupId = useId();
  return (
    <div
      role="tablist"
      className={cn(
        "inline-flex items-center gap-1 rounded-full border border-line bg-canvas p-1",
        className,
      )}
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            role="tab"
            aria-selected={selected}
            onClick={() => onChange(option.value)}
            className={cn(
              "relative rounded-full font-medium transition-colors duration-200",
              size === "sm" ? "px-3 py-1 text-xs" : "px-4 py-1.5 text-sm",
              selected ? "text-ink-900" : "text-ink-400 hover:text-ink-700",
            )}
          >
            {selected && (
              <motion.span
                layoutId={`segment-${groupId}`}
                transition={{ type: "spring", stiffness: 420, damping: 34 }}
                className="absolute inset-0 rounded-full bg-paper shadow-soft"
              />
            )}
            <span className="relative">{option.label}</span>
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ Alert */

export function Alert({
  tone = "neutral",
  title,
  children,
  className,
  ...rest
}: {
  tone?: Tone;
  title?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
} & React.HTMLAttributes<HTMLDivElement>) {
  const skin: Record<Tone, string> = {
    brand: "border-navy-100 bg-navy-50 text-ink-800",
    azure: "border-azure-100 bg-azure-50 text-azure-800",
    emerald: "border-ok-100 bg-ok-50 text-ok-600",
    amber: "border-warn-100 bg-warn-50 text-warn-600",
    rose: "border-bad-100 bg-bad-50 text-bad-600",
    neutral: "border-line bg-canvas text-ink-600",
  };
  return (
    <div className={cn("rounded-xl border px-4 py-3 text-sm", skin[tone], className)} {...rest}>
      {title && <p className="mb-0.5 font-semibold">{title}</p>}
      <div className="[&_a]:underline">{children}</div>
    </div>
  );
}

/* -------------------------------------------------------------- Accordion */

/** One-open-at-a-time disclosure list, used by the landing FAQ. */
export function Accordion({
  items,
  className,
}: {
  items: ReadonlyArray<{ q: string; a: React.ReactNode }>;
  className?: string;
}) {
  const [open, setOpen] = useState<number | null>(0);
  return (
    <div className={cn("divide-y divide-line overflow-hidden rounded-panel border border-line bg-paper shadow-card", className)}>
      {items.map((item, index) => {
        const expanded = open === index;
        return (
          <div key={item.q}>
            <button
              type="button"
              aria-expanded={expanded}
              onClick={() => setOpen(expanded ? null : index)}
              className="flex w-full items-center justify-between gap-4 px-5 py-4 text-left transition-colors hover:bg-canvas sm:px-6 sm:py-5"
            >
              <span className="text-[15px] font-medium text-ink-900">{item.q}</span>
              <span
                className={cn(
                  "grid size-7 flex-none place-items-center rounded-full border border-line text-ink-500 transition-all duration-300",
                  expanded && "rotate-180 border-azure-200 bg-azure-50 text-azure-700",
                )}
              >
                <IconChevronDown style={{ height: 15, width: 15 }} />
              </span>
            </button>
            <AnimatePresence initial={false}>
              {expanded && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}
                  className="overflow-hidden"
                >
                  <div className="px-5 pb-5 text-sm leading-relaxed text-ink-500 sm:px-6">
                    {item.a}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        );
      })}
    </div>
  );
}

/* ----------------------------------------------------------------- Motion */

/** Staggered entrance for lists of cards (React Bits "Animated List"). */
export function Rise({
  delay = 0,
  className,
  children,
}: {
  delay?: number;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.45, delay, ease: [0.22, 1, 0.36, 1] }}
      className={className}
    >
      {children}
    </motion.div>
  );
}

/** Same entrance, but triggered when the element scrolls into view. */
export function Reveal({
  delay = 0,
  className,
  children,
}: {
  delay?: number;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 22 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-80px" }}
      transition={{ duration: 0.6, delay, ease: [0.22, 1, 0.36, 1] }}
      className={className}
    >
      {children}
    </motion.div>
  );
}

export function FadeSwap({ keyed, children }: { keyed: string; children: React.ReactNode }) {
  return (
    <AnimatePresence mode="wait" initial={false}>
      <motion.div
        key={keyed}
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: -6 }}
        transition={{ duration: 0.18 }}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}

/* ------------------------------------------------------------- Empty state */

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: React.ReactNode;
  title: string;
  description?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
      {icon && (
        <div className="mb-1 grid size-12 place-items-center rounded-2xl border border-line bg-canvas text-azure-600">
          {icon}
        </div>
      )}
      <p className="text-[15px] font-semibold text-ink-900">{title}</p>
      {description && <p className="max-w-sm text-sm text-ink-500">{description}</p>}
      {action}
    </div>
  );
}
