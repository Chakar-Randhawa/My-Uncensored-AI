"use client";

import { useState, useRef, useEffect } from "react";
import { PROVIDERS, type ProviderId } from "@/lib/types";

interface ProviderSelectorProps {
  value: ProviderId[] | null; // null = race mode (all providers)
  onChange: (value: ProviderId[] | null) => void;
}

export function ProviderSelector({ value, onChange }: ProviderSelectorProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const label = value === null ? "Race mode" : PROVIDERS.find((p) => p.id === value[0])?.label ?? value[0];

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground hover:bg-surface"
      >
        <span className={`h-1.5 w-1.5 rounded-full ${value === null ? "bg-accent" : "bg-muted-foreground"}`} />
        {label}
      </button>

      {open && (
        <div className="absolute right-0 top-full z-20 mt-1 w-56 rounded-md border border-border bg-surface py-1 shadow-md">
          <button
            onClick={() => {
              onChange(null);
              setOpen(false);
            }}
            className={`flex w-full flex-col items-start px-3 py-2 text-left text-sm hover:bg-muted ${
              value === null ? "text-accent" : ""
            }`}
          >
            <span className="font-medium">Race mode</span>
            <span className="text-xs text-muted-foreground">
              All configured providers compete — fastest wins
            </span>
          </button>

          <div className="my-1 border-t border-border" />

          {PROVIDERS.map((provider) => (
            <button
              key={provider.id}
              onClick={() => {
                onChange([provider.id]);
                setOpen(false);
              }}
              className={`flex w-full items-center px-3 py-2 text-left text-sm hover:bg-muted ${
                value?.[0] === provider.id ? "text-accent" : ""
              }`}
            >
              {provider.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
