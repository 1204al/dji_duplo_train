import { useState } from "react";
import { CaretDown, Check, Copy } from "@phosphor-icons/react";

// Small shared building blocks.

export function Switch({ checked, onChange, disabled, title, children }) {
  return (
    <button
      type="button"
      role="switch"
      className="switch"
      aria-checked={checked}
      disabled={disabled}
      title={title}
      onClick={() => onChange(!checked)}
    >
      <span className="switch-track" aria-hidden="true" />
      {children}
    </button>
  );
}

// Collapsible card for optional features. `defaultOpen` is read once.
export function Panel({
  title,
  subtitle,
  status,
  defaultOpen = false,
  children,
}) {
  const [initiallyOpen] = useState(defaultOpen);
  return (
    <details className="card panel" open={initiallyOpen}>
      <summary>
        <span className="panel-title">
          <span className="panel-name">
            <strong>{title}</strong>
            {status}
          </span>
          <span>{subtitle}</span>
        </span>
        <CaretDown className="caret" size={18} />
      </summary>
      <div className="panel-body">{children}</div>
    </details>
  );
}

export function CopyButton({ value, label }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="icon-button"
      title={`Copy ${label}`}
      aria-label={`Copy ${label}`}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(String(value));
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          setCopied(false);
        }
      }}
    >
      {copied ? <Check size={16} /> : <Copy size={16} />}
    </button>
  );
}

// localStorage can throw (private windows, blocked storage).
export function readStored(key, fallback = "") {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

export function writeStored(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* not critical */
  }
}
