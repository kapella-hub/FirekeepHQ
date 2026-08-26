interface FirekeepMarkProps {
  readonly size?: number;
  readonly className?: string;
}

/** The id-free, currentColor Firekeep Beacon used by every in-app brand surface. */
export function FirekeepMark({ size = 24, className }: FirekeepMarkProps): React.JSX.Element {
  return (
    <svg
      aria-hidden="true"
      className={className}
      focusable="false"
      height={size}
      viewBox="0 0 64 64"
      width={size}
    >
      <path d="M14 35 A18 18 0 0 0 50 35 Z" fill="currentColor" />
      <rect fill="currentColor" height="4" rx="2" width="6" x="29" y="49" />
      <g transform="translate(17.90 5.77) scale(0.92)">
        <path
          d="M17.61 5.58C18.13 9.84 21.87 11.75 21.87 16.62C21.87 21.06 19.00 24.28 15.35 24.28C11.61 24.28 9.00 21.58 9.00 17.84C9.00 14.62 11.34 12.71 12.39 10.01C12.82 12.62 13.95 13.75 15.00 14.28C14.39 10.88 15.43 8.10 17.61 5.58Z M16.30 14.54C16.65 16.71 18.48 17.76 18.48 19.67C18.48 21.41 17.17 22.63 15.43 22.63C13.69 22.63 12.39 21.50 12.39 19.93C12.39 18.63 13.26 17.84 13.95 16.62C14.13 17.76 14.56 18.28 15.09 18.54C14.82 17.15 15.26 15.93 16.30 14.54Z"
          fill="currentColor"
          fillRule="evenodd"
        />
      </g>
    </svg>
  );
}
