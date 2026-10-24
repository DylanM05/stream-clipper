import type { ReactNode } from "react";

type IconProps = { className?: string };

function outline(paths: ReactNode) {
  return function Icon({ className = "size-5" }: IconProps) {
    return (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={className}
        aria-hidden="true"
      >
        {paths}
      </svg>
    );
  };
}

function filled(paths: ReactNode) {
  return function Icon({ className = "size-5" }: IconProps) {
    return (
      <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true">
        {paths}
      </svg>
    );
  };
}

export const CheckIcon = outline(<path d="M5 12.5l4.5 4.5L19 7" />);
export const XIcon = outline(<path d="M6 6l12 12M18 6L6 18" />);
export const BackIcon = outline(<path d="M15 18l-6-6 6-6" />);
export const PlayIcon = filled(<path d="M8 5.5v13a1 1 0 0 0 1.5.86l11-6.5a1 1 0 0 0 0-1.72l-11-6.5A1 1 0 0 0 8 5.5z" />);
export const PauseIcon = filled(<path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" />);
export const InboxIcon = outline(
  <>
    <path d="M3 13h5l1.5 3h5l1.5-3h5" />
    <path d="M5.5 5h13L21 13v6H3v-6z" />
  </>,
);
export const FilmIcon = outline(
  <>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4" />
  </>,
);
export const DoneIcon = outline(
  <>
    <circle cx="12" cy="12" r="9" />
    <path d="M8 12.5l3 3 5-6" />
  </>,
);
export const DownloadIcon = outline(
  <>
    <path d="M12 4v11" />
    <path d="M7 10l5 5 5-5" />
    <path d="M5 20h14" />
  </>,
);
export const PhoneIcon = outline(
  <>
    <rect x="7" y="3" width="10" height="18" rx="2" />
    <path d="M11 18h2" />
  </>,
);
export const SparkIcon = outline(<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" />);
export const VolumeIcon = outline(
  <>
    <path d="M11 5L6 9H3v6h3l5 4z" />
    <path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13" />
  </>,
);
export const MutedIcon = outline(
  <>
    <path d="M11 5L6 9H3v6h3l5 4z" />
    <path d="M16 9.5l5 5M21 9.5l-5 5" />
  </>,
);
export const TvIcon = outline(
  <>
    <rect x="3" y="7" width="18" height="13" rx="2" />
    <path d="M8 3l4 4 4-4" />
  </>,
);
export const RefreshIcon = outline(
  <>
    <path d="M20 11a8 8 0 0 0-14.5-4.5L4 8" />
    <path d="M4 3.5V8h4.5" />
    <path d="M4 13a8 8 0 0 0 14.5 4.5L20 16" />
    <path d="M20 20.5V16h-4.5" />
  </>,
);
export const UploadIcon = outline(
  <>
    <path d="M12 15V4" />
    <path d="M7 9l5-5 5 5" />
    <path d="M5 20h14" />
  </>,
);
export const ShareIcon = outline(
  <>
    <path d="M12 3v12" />
    <path d="M8 7l4-4 4 4" />
    <path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7" />
  </>,
);
export const CopyIcon = outline(
  <>
    <rect x="8" y="8" width="12" height="12" rx="2" />
    <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
  </>,
);
export const TrashIcon = outline(
  <>
    <path d="M4 7h16M10 11v6M14 11v6" />
    <path d="M6 7l1 13h10l1-13M9 7V4h6v3" />
  </>,
);
export const LinkIcon = outline(
  <>
    <path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1" />
    <path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1" />
  </>,
);
