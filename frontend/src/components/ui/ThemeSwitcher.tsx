import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";

import { useTheme, type ThemePreference } from "../../hooks/useTheme";
import { cn } from "../../lib/cn";
import { IconCheck, IconMonitor, IconMoon, IconSun } from "./Icons";

const OPTIONS = [
  { value: "system", label: "System", icon: IconMonitor },
  { value: "light", label: "Light", icon: IconSun },
  { value: "dark", label: "Dark", icon: IconMoon },
] as const;

export function ThemeSwitcher() {
  const { preference, resolvedTheme, setPreference } = useTheme();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const CurrentIcon = resolvedTheme === "dark" ? IconMoon : IconSun;

  useEffect(() => {
    if (!open) return;

    const closeOnOutsideClick = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };

    document.addEventListener("pointerdown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const choose = (next: ThemePreference) => {
    setPreference(next);
    setOpen(false);
  };

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-label={`Theme: ${preference}. Choose appearance`}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Choose appearance"
        className={cn(
          "grid size-9 place-items-center rounded-full border border-line bg-paper text-ink-500 shadow-soft",
          "transition-all duration-200 hover:border-azure-300 hover:bg-azure-50 hover:text-azure-700",
          open && "border-azure-300 bg-azure-50 text-azure-700",
        )}
      >
        <AnimatePresence mode="wait" initial={false}>
          <motion.span
            key={resolvedTheme}
            initial={{ opacity: 0, rotate: -20, scale: 0.75 }}
            animate={{ opacity: 1, rotate: 0, scale: 1 }}
            exit={{ opacity: 0, rotate: 20, scale: 0.75 }}
            transition={{ duration: 0.16 }}
          >
            <CurrentIcon style={{ height: 17, width: 17 }} />
          </motion.span>
        </AnimatePresence>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            role="menu"
            aria-label="Appearance"
            initial={{ opacity: 0, y: -5, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.98 }}
            transition={{ duration: 0.16, ease: [0.22, 1, 0.36, 1] }}
            className="absolute right-0 top-11 z-[70] w-44 overflow-hidden rounded-2xl border border-line bg-paper p-1.5 shadow-lift"
          >
            {OPTIONS.map(({ value, label, icon: Icon }) => {
              const selected = preference === value;
              return (
                <button
                  key={value}
                  type="button"
                  role="menuitemradio"
                  aria-checked={selected}
                  onClick={() => choose(value)}
                  className={cn(
                    "flex w-full items-center gap-2.5 rounded-xl px-3 py-2 text-left text-sm transition-colors",
                    selected
                      ? "bg-azure-50 font-medium text-azure-700"
                      : "text-ink-600 hover:bg-canvas hover:text-ink-900",
                  )}
                >
                  <Icon className="flex-none" style={{ height: 16, width: 16 }} />
                  <span className="flex-1">{label}</span>
                  {selected && <IconCheck style={{ height: 14, width: 14 }} />}
                </button>
              );
            })}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
