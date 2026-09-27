interface Window {
  /** Chromium's File System Access API; absent in browsers that do not support folder picking. */
  showDirectoryPicker?: (options?: {
    mode?: "read" | "readwrite";
  }) => Promise<FileSystemDirectoryHandle>;
}
