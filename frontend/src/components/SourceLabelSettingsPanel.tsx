import { useEffect, useMemo, useState } from "react";

import { ApiError, api } from "../api/client";
import type {
  SourceLabelFontPreset,
  SourceLabelSettings,
  SourceLabelStyle,
} from "../api/types";
import { cn } from "../lib/cn";
import { Button } from "./ui/Button";
import { IconCheck, IconFilm, IconRefresh } from "./ui/Icons";
import { Alert, Field, Input, Panel, PanelHeader, Rise } from "./ui/Primitives";

const DEFAULT_STYLE: SourceLabelStyle = {
  fontPreset: "bebas-neue",
  fillColor: "#FFFFFF",
  outlineColor: "#000000",
  sizePercent: 4,
};

const FONT_OPTIONS: Array<{
  value: SourceLabelFontPreset;
  label: string;
  family: string;
}> = [
  { value: "bebas-neue", label: "Bebas Neue", family: "Source Bebas Neue" },
  { value: "anton", label: "Anton", family: "Source Anton" },
  { value: "oswald-semibold", label: "Oswald", family: "Source Oswald" },
  {
    value: "roboto-condensed-bold",
    label: "Roboto Condensed",
    family: "Source Roboto Condensed",
  },
];

const COLOR_PATTERN = /^#[0-9A-Fa-f]{6}$/;

function fontFamily(preset: SourceLabelFontPreset): string {
  return FONT_OPTIONS.find((option) => option.value === preset)?.family ?? FONT_OPTIONS[0]!.family;
}

function ColorControl({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const valid = COLOR_PATTERN.test(value);
  return (
    <Field label={label} error={valid ? undefined : "Enter a six-digit color such as #FFFFFF."}>
      <div className="flex items-center gap-2">
        <input
          type="color"
          aria-label={`${label} picker`}
          value={valid ? value : "#000000"}
          onChange={(event) => onChange(event.target.value.toUpperCase())}
          className="h-10 w-12 cursor-pointer rounded-lg border border-line bg-paper p-1"
        />
        <Input
          value={value}
          maxLength={7}
          spellCheck={false}
          aria-label={`${label} hex value`}
          onChange={(event) => onChange(event.target.value.toUpperCase())}
          className="font-mono uppercase"
        />
        {["#FFFFFF", "#000000"].map((color) => (
          <button
            key={color}
            type="button"
            aria-label={`Use ${color} for ${label.toLowerCase()}`}
            title={color}
            onClick={() => onChange(color)}
            className="size-9 flex-none rounded-lg border border-line-strong shadow-soft"
            style={{ backgroundColor: color }}
          />
        ))}
      </div>
    </Field>
  );
}

export function SourceLabelSettingsPanel() {
  const [saved, setSaved] = useState<SourceLabelSettings | null>(null);
  const [style, setStyle] = useState<SourceLabelStyle>(DEFAULT_STYLE);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void api
      .sourceLabelSettings()
      .then((next) => {
        if (!active) return;
        setSaved(next);
        setStyle(next);
      })
      .catch((caught) => {
        if (active) {
          setError(caught instanceof ApiError ? caught.message : "Could not load source-label settings.");
        }
      });
    return () => {
      active = false;
    };
  }, []);

  const valid =
    COLOR_PATTERN.test(style.fillColor) &&
    COLOR_PATTERN.test(style.outlineColor) &&
    style.sizePercent >= 2.5 &&
    style.sizePercent <= 8 &&
    Number.isInteger(style.sizePercent * 4);

  const dirty = useMemo(() => {
    if (!saved) return false;
    return (
      saved.fontPreset !== style.fontPreset ||
      saved.fillColor !== style.fillColor ||
      saved.outlineColor !== style.outlineColor ||
      saved.sizePercent !== style.sizePercent
    );
  }, [saved, style]);

  const persist = async (next: SourceLabelStyle, message: string) => {
    setBusy(true);
    setStatus(null);
    setError(null);
    try {
      const response = await api.saveSourceLabelSettings(next);
      setSaved(response);
      setStyle(response);
      setStatus(message);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The source-label settings could not be saved.");
    } finally {
      setBusy(false);
    }
  };

  const previewFamily = fontFamily(style.fontPreset);

  return (
    <Rise>
      <Panel>
        <PanelHeader
          title={
            <span className="flex items-center gap-2">
              <IconFilm style={{ height: 16, width: 16 }} />
              Source label
            </span>
          }
          description="Default typography for new jobs. Each job keeps the style it started with."
        />

        <div
          aria-label="Source label preview"
          className="relative mb-6 aspect-video w-full overflow-hidden rounded-xl border border-line-strong"
          style={{
            containerType: "inline-size",
            background:
              "linear-gradient(125deg, #e8edf4 0%, #8b9aab 34%, #182238 35%, #050a14 68%, #d5dce6 69%, #fafcff 100%)",
          }}
        >
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_70%_40%,rgb(0_163_250_/_0.18),transparent_34%)]" />
          <span
            className="absolute whitespace-nowrap uppercase leading-none"
            style={{
              left: "0.75%",
              top: "1%",
              color: COLOR_PATTERN.test(style.fillColor) ? style.fillColor : "#FFFFFF",
              fontFamily: `"${previewFamily}"`,
              fontSize: `${style.sizePercent * 0.5625}cqw`,
              WebkitTextStroke: `max(1px, ${style.sizePercent * 0.034}cqw) ${
                COLOR_PATTERN.test(style.outlineColor) ? style.outlineColor : "#000000"
              }`,
              paintOrder: "stroke fill",
            }}
          >
            DRIVER SPHERE
          </span>
        </div>

        <Field label="Font style" hint="All presets are bundled and render identically in previews and exports.">
          <div className="grid gap-2 sm:grid-cols-2">
            {FONT_OPTIONS.map((option) => {
              const selected = style.fontPreset === option.value;
              return (
                <button
                  key={option.value}
                  type="button"
                  aria-label={`${option.label} font`}
                  aria-pressed={selected}
                  onClick={() => setStyle((current) => ({ ...current, fontPreset: option.value }))}
                  className={cn(
                    "flex items-center justify-between rounded-xl border px-4 py-3 text-left transition-colors",
                    selected
                      ? "border-azure-400 bg-azure-50 text-ink-900"
                      : "border-line bg-canvas text-ink-600 hover:border-line-strong",
                  )}
                >
                  <span>
                    <span
                      className="block text-lg leading-none"
                      style={{ fontFamily: `"${option.family}"` }}
                    >
                      SOURCE NAME
                    </span>
                    <span className="mt-1 block text-xs font-medium">{option.label}</span>
                  </span>
                  {selected && <IconCheck style={{ height: 15, width: 15 }} />}
                </button>
              );
            })}
          </div>
        </Field>

        <div className="mt-5 grid gap-4 sm:grid-cols-2">
          <ColorControl
            label="Text color"
            value={style.fillColor}
            onChange={(fillColor) => setStyle((current) => ({ ...current, fillColor }))}
          />
          <ColorControl
            label="Outline color"
            value={style.outlineColor}
            onChange={(outlineColor) => setStyle((current) => ({ ...current, outlineColor }))}
          />
        </div>

        <Field
          label="Responsive size"
          hint={`${style.sizePercent.toFixed(2).replace(/\.00$/, "")}% of video height. Long names shrink only when needed to stay on one line.`}
          className="mt-5"
        >
          <div className="flex items-center gap-4">
            <input
              type="range"
              aria-label="Responsive size slider"
              min={2.5}
              max={8}
              step={0.25}
              value={style.sizePercent}
              onChange={(event) =>
                setStyle((current) => ({ ...current, sizePercent: Number(event.target.value) }))
              }
              className="flex-1"
            />
            <Input
              type="number"
              aria-label="Responsive size percentage"
              min={2.5}
              max={8}
              step={0.25}
              value={style.sizePercent}
              onChange={(event) =>
                setStyle((current) => ({ ...current, sizePercent: Number(event.target.value) }))
              }
              className="w-24 text-center tabular-nums"
            />
          </div>
        </Field>

        {status && (
          <Alert tone="emerald" className="mt-5">
            <span className="flex gap-2">
              <IconCheck className="mt-0.5 flex-none" style={{ height: 14, width: 14 }} />
              {status}
            </span>
          </Alert>
        )}
        {error && (
          <Alert tone="rose" className="mt-5" role="alert">
            {error}
          </Alert>
        )}

        <div className="mt-6 flex flex-wrap gap-2">
          <Button
            variant="primary"
            loading={busy}
            disabled={busy || !valid || !dirty}
            onClick={() => void persist(style, "Source-label defaults saved for new jobs.")}
          >
            Save source label
          </Button>
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => void persist(DEFAULT_STYLE, "Source-label defaults restored.")}
          >
            <IconRefresh style={{ height: 14, width: 14 }} />
            Reset defaults
          </Button>
        </div>
      </Panel>
    </Rise>
  );
}

