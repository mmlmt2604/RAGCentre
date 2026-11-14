import type { LucideIcon } from "lucide-react";
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Link, type LinkProps } from "react-router";

type Variant = "primary" | "secondary" | "ghost" | "danger";

interface Common {
  variant?: Variant;
  size?: "sm" | "md";
  icon?: LucideIcon;
  loading?: boolean;
  children?: ReactNode;
}

function classes(variant: Variant, size: "sm" | "md", extra?: string) {
  return ["rc-btn", `rc-btn--${variant}`, size === "sm" ? "rc-btn--sm" : "", extra ?? ""].filter(Boolean).join(" ");
}

export const Button = forwardRef<HTMLButtonElement, Common & ButtonHTMLAttributes<HTMLButtonElement>>(function Button(
  { variant = "secondary", size = "md", icon: Icon, loading = false, children, className, disabled, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={classes(variant, size, className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <span className="rc-spinner" aria-hidden /> : Icon ? <Icon size={14} strokeWidth={1.75} aria-hidden /> : null}
      {children}
    </button>
  );
});

export function ButtonLink({ variant = "secondary", size = "md", icon: Icon, children, className, ...rest }: Omit<Common, "loading"> & LinkProps) {
  return (
    <Link className={classes(variant, size, className)} {...rest}>
      {Icon ? <Icon size={14} strokeWidth={1.75} aria-hidden /> : null}
      {children}
    </Link>
  );
}

/** An icon-only button. `label` is required: it becomes the accessible name and tooltip. */
export function IconButton({ icon: Icon, label, ...rest }: { icon: LucideIcon; label: string } & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button type="button" className="rc-iconbtn" aria-label={label} title={label} {...rest}>
      <Icon size={16} strokeWidth={1.75} aria-hidden />
    </button>
  );
}
