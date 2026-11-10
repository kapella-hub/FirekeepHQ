interface FirekeepMarkProps {
  readonly size?: number;
  readonly className?: string;
}

/**
 * The id-free, currentColor Firekeep mark used by every in-app brand surface: the kept
 * fire, five lines of light rising from a hearth plate. Same drawing as firekeep.ai's
 * `brand/mark.svg`, with slightly heavier strokes because it is shown at 14-24px here.
 */
export function FirekeepMark({ size = 24, className }: FirekeepMarkProps): React.JSX.Element {
  return (
    <svg
      aria-hidden="true"
      className={className}
      focusable="false"
      height={size}
      viewBox="0 0 48 48"
      width={size}
    >
      <ellipse cx="24" cy="39.6" fill="none" rx="18.6" ry="4.7" stroke="currentColor" strokeWidth="2.6" />
      <path
        d="M11.5 38.5Q11.5 31.1 18.5 25 M17.5 38.5Q17.5 25.6 20.5 15 M24 38.5Q24 20.3 24.8 5.5 M30.5 38.5Q30.5 26.7 28.2 17 M36.5 38.5Q36.5 32.2 30.5 27"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="2.8"
      />
      <circle cx="18.5" cy="25" fill="currentColor" r="2.1" />
      <circle cx="20.5" cy="15" fill="currentColor" r="2.1" />
      <circle cx="24.8" cy="5.5" fill="currentColor" r="2.1" />
      <circle cx="28.2" cy="17" fill="currentColor" r="2.1" />
      <circle cx="30.5" cy="27" fill="currentColor" r="2.1" />
    </svg>
  );
}
