import { webcrypto } from "node:crypto";

import "@testing-library/jest-dom/vitest";

// jsdom's crypto has no `subtle`; the uploader needs it for chunk checksums.
if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, "crypto", { value: webcrypto, configurable: true });
}

// jsdom does not implement Blob.prototype.arrayBuffer, which every browser
// does. File.slice(...).arrayBuffer() is how the uploader reads each chunk.
if (typeof Blob !== "undefined" && typeof Blob.prototype.arrayBuffer !== "function") {
  Object.defineProperty(Blob.prototype, "arrayBuffer", {
    configurable: true,
    writable: true,
    value(this: Blob): Promise<ArrayBuffer> {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as ArrayBuffer);
        reader.onerror = () => reject(reader.error);
        reader.readAsArrayBuffer(this);
      });
    },
  });
}
