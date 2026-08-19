import { forwardRef, useImperativeHandle, useRef } from "react";

export interface FileTriggerHandle {
  /** Opens the OS picker. Call it from whatever button the surface shows. */
  open: () => void;
  /** Clears the selection, so picking the same file twice fires twice. */
  reset: () => void;
}

interface FileTriggerProps {
  accept: string;
  onFiles: (files: FileList) => void;
  multiple?: boolean;
}

/** The hidden `<input type="file">` behind an attach button.
 *
 * Never visible — the two composers each show their own paperclip and reach in
 * to `.click()` it. Both had also learned the same non-obvious half of the
 * contract: the input keeps its value, so re-picking the file you just removed
 * fires no change event, and each surface had scattered `.value = ''` across
 * three call sites to work around it. That is `reset()` now, and it is the
 * component's problem rather than a thing to remember.
 */
export const FileTrigger = forwardRef<FileTriggerHandle, FileTriggerProps>(
  function FileTrigger({ accept, onFiles, multiple = false }, ref) {
    const inputRef = useRef<HTMLInputElement | null>(null);
    useImperativeHandle(ref, () => ({
      open: () => inputRef.current?.click(),
      reset: () => {
        if (inputRef.current) inputRef.current.value = "";
      },
    }));
    return (
      <input
        ref={inputRef}
        type="file"
        className="file-trigger"
        accept={accept}
        multiple={multiple}
        onChange={(e) => {
          if (e.target.files?.length) onFiles(e.target.files);
        }}
      />
    );
  },
);
