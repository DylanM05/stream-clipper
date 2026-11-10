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
export const ChartIcon = outline(
  <>
    <path d="M4 20h16" />
    <path d="M7 16v-5M12 16V6M17 16v-8" />
  </>,
);
export const LinkIcon = outline(
  <>
    <path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1" />
    <path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1" />
  </>,
);
export const GridIcon = outline(
  <>
    <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" />
    <rect x="13.5" y="3.5" width="7" height="7" rx="1.5" />
    <rect x="3.5" y="13.5" width="7" height="7" rx="1.5" />
    <rect x="13.5" y="13.5" width="7" height="7" rx="1.5" />
  </>,
);
export const AlertIcon = outline(
  <>
    <path d="M12 4l9 16H3z" />
    <path d="M12 10v4M12 17h.01" />
  </>,
);
export const UndoIcon = outline(
  <>
    <path d="M9 14L4 9l5-5" />
    <path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" />
  </>,
);
export const RedoIcon = outline(
  <>
    <path d="M15 14l5-5-5-5" />
    <path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13" />
  </>,
);
export const ScissorsIcon = outline(
  <>
    <circle cx="6" cy="6" r="3" />
    <circle cx="6" cy="18" r="3" />
    <path d="M20 4L8.1 15.9M14.5 14.5L20 20M8.1 8.1L12 12" />
  </>,
);
export const LoopIcon = outline(
  <>
    <path d="M17 2l4 4-4 4" />
    <path d="M3 11V9a3 3 0 0 1 3-3h15" />
    <path d="M7 22l-4-4 4-4" />
    <path d="M21 13v2a3 3 0 0 1-3 3H3" />
  </>,
);
export const MagnetIcon = outline(
  <>
    <path d="M6 3v8a6 6 0 0 0 12 0V3" />
    <path d="M6 3h4v8a2 2 0 0 0 4 0V3h4" />
    <path d="M6 7h4M14 7h4" />
  </>,
);
export const KeyboardIcon = outline(
  <>
    <rect x="2.5" y="6" width="19" height="12" rx="2" />
    <path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10" />
  </>,
);
export const ToStartIcon = outline(
  <>
    <path d="M6 5v14" />
    <path d="M18 6l-8 6 8 6z" />
  </>,
);
export const ToEndIcon = outline(
  <>
    <path d="M18 5v14" />
    <path d="M6 6l8 6-8 6z" />
  </>,
);
export const FrameBackIcon = outline(<path d="M15 6l-6 6 6 6" />);
export const FrameForwardIcon = outline(<path d="M9 6l6 6-6 6" />);
export const ZoomInIcon = outline(
  <>
    <circle cx="11" cy="11" r="6.5" />
    <path d="M20.5 20.5l-4.9-4.9M8 11h6M11 8v6" />
  </>,
);
export const ZoomOutIcon = outline(
  <>
    <circle cx="11" cy="11" r="6.5" />
    <path d="M20.5 20.5l-4.9-4.9M8 11h6" />
  </>,
);
export const FitIcon = outline(<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" />);
export const PlusIcon = outline(<path d="M12 5v14M5 12h14" />);
export const ChevronIcon = outline(<path d="M6 9l6 6 6-6" />);
export const FolderIcon = outline(
  <path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l2 2.5h8.5A1.5 1.5 0 0 1 21 10v8.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z" />,
);
export const LibraryIcon = outline(
  <>
    <rect x="4" y="4" width="4" height="16" rx="1" />
    <rect x="10" y="4" width="4" height="16" rx="1" />
    <path d="M16.2 5.3l2.9-.8 3.4 14.5-2.9.8z" />
  </>,
);
export const GearIcon = outline(
  <>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </>,
);
