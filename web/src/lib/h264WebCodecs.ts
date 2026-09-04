/**
 * Annex-B H.264 (scrcpy raw_stream) → AVCC → WebCodecs VideoDecoder → canvas.
 *
 * Chrome's VideoDecoder with `avc1.*` expects length-prefixed AVCC NALs plus an
 * `avcC` description — NOT Annex-B start codes. We convert on the fly.
 */

export type H264PaintHandler = (frame: VideoFrame) => void;

const NAL_NON_IDR = 1;
const NAL_IDR = 5;
const NAL_SPS = 7;
const NAL_PPS = 8;

function findStartCode(data: Uint8Array, from: number): { index: number; len: number } | null {
  for (let i = from; i + 3 < data.length; i++) {
    if (data[i] === 0 && data[i + 1] === 0) {
      if (data[i + 2] === 1) return { index: i, len: 3 };
      if (data[i + 2] === 0 && data[i + 3] === 1) return { index: i, len: 4 };
    }
  }
  return null;
}

function nalTypeOf(nalNoStartCode: Uint8Array): number {
  if (nalNoStartCode.length === 0) return -1;
  return nalNoStartCode[0] & 0x1f;
}

/** Build codec string from SPS NAL (without start code). */
export function codecStringFromSps(sps: Uint8Array): string {
  if (sps.length < 4) return "avc1.42E01E";
  const profile = sps[1].toString(16).padStart(2, "0");
  const compat = sps[2].toString(16).padStart(2, "0");
  const level = sps[3].toString(16).padStart(2, "0");
  return `avc1.${profile}${compat}${level}`;
}

/** Build avcC (AVCDecoderConfigurationRecord) from SPS/PPS NALs (no start codes). */
export function buildAvcC(sps: Uint8Array, pps: Uint8Array): Uint8Array {
  const out = new Uint8Array(11 + sps.length + 3 + pps.length);
  let o = 0;
  out[o++] = 1; // configurationVersion
  out[o++] = sps[1]; // AVCProfileIndication
  out[o++] = sps[2]; // profile_compatibility
  out[o++] = sps[3]; // AVCLevelIndication
  out[o++] = 0xff; // lengthSizeMinusOne = 3 (4-byte lengths)
  out[o++] = 0xe1; // numOfSequenceParameterSets = 1
  out[o++] = (sps.length >> 8) & 0xff;
  out[o++] = sps.length & 0xff;
  out.set(sps, o);
  o += sps.length;
  out[o++] = 1; // numOfPictureParameterSets
  out[o++] = (pps.length >> 8) & 0xff;
  out[o++] = pps.length & 0xff;
  out.set(pps, o);
  return out;
}

/** Concatenate NALs as AVCC (4-byte big-endian length + NAL). */
function toAvcc(nals: Uint8Array[]): Uint8Array {
  let total = 0;
  for (const n of nals) total += 4 + n.length;
  const out = new Uint8Array(total);
  let o = 0;
  for (const n of nals) {
    const len = n.length;
    out[o++] = (len >>> 24) & 0xff;
    out[o++] = (len >>> 16) & 0xff;
    out[o++] = (len >>> 8) & 0xff;
    out[o++] = len & 0xff;
    out.set(n, o);
    o += len;
  }
  return out;
}

export function isWebCodecsSupported(): boolean {
  return typeof VideoDecoder !== "undefined";
}

export class H264WebCodecsPlayer {
  private decoder: VideoDecoder | null = null;
  private buffer = new Uint8Array(0);
  private sps: Uint8Array | null = null; // without start code
  private pps: Uint8Array | null = null;
  private configured = false;
  private codecString: string;
  private onFrame: H264PaintHandler;
  private timestampUs = 0;
  private closed = false;
  private seenKey = false;

  constructor(onFrame: H264PaintHandler, codecString = "avc1.42E01E") {
    this.onFrame = onFrame;
    this.codecString = codecString;
  }

  setCodecString(codecString: string) {
    if (codecString && codecString !== this.codecString && !this.configured) {
      this.codecString = codecString;
    }
  }

  push(chunk: ArrayBuffer | Uint8Array) {
    if (this.closed) return;
    const incoming = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
    const merged = new Uint8Array(this.buffer.length + incoming.length);
    merged.set(this.buffer, 0);
    merged.set(incoming, this.buffer.length);
    this.buffer = merged;
    this._drain();
  }

  close() {
    this.closed = true;
    try {
      this.decoder?.close();
    } catch {
      // ignore
    }
    this.decoder = null;
    this.buffer = new Uint8Array(0);
  }

  private _ensureDecoder() {
    if (this.decoder) return;
    if (!isWebCodecsSupported()) {
      throw new Error("WebCodecs VideoDecoder unavailable");
    }
    this.decoder = new VideoDecoder({
      output: (frame) => {
        try {
          this.onFrame(frame);
        } finally {
          frame.close();
        }
      },
      error: (err) => {
        console.warn("H264 VideoDecoder error", err);
        this.configured = false;
        try {
          this.decoder?.close();
        } catch {
          // ignore
        }
        this.decoder = null;
      },
    });
  }

  private _configureIfNeeded(isKey: boolean) {
    if (this.configured || !this.sps || !this.pps) return;
    if (!isKey) return;
    this._ensureDecoder();
    if (!this.decoder) return;

    const codec = codecStringFromSps(this.sps);
    this.codecString = codec;
    const description = buildAvcC(this.sps, this.pps);
    try {
      this.decoder.configure({
        codec,
        description,
        optimizeForLatency: true,
      });
      this.configured = true;
      this.seenKey = false;
    } catch (err) {
      console.warn("H264 VideoDecoder.configure failed", codec, err);
      this.configured = false;
    }
  }

  private _emitAu(vclNals: Uint8Array[], isKey: boolean) {
    this._configureIfNeeded(isKey);
    if (!this.configured || !this.decoder) return;
    // Wait for a keyframe after configure before feeding deltas.
    if (!this.seenKey) {
      if (!isKey) return;
      this.seenKey = true;
    }

    const data = toAvcc(vclNals);
    const chunk = new EncodedVideoChunk({
      type: isKey ? "key" : "delta",
      timestamp: this.timestampUs,
      data,
    });
    this.timestampUs += 33_333;
    try {
      if (this.decoder.decodeQueueSize > 10) {
        // Drop backlog under pressure; wait for next keyframe.
        this.seenKey = false;
        return;
      }
      this.decoder.decode(chunk);
    } catch (err) {
      console.warn("H264 decode() failed", err);
      this.seenKey = false;
    }
  }

  private _drain() {
    const data = this.buffer;
    const starts: { index: number; len: number }[] = [];
    let pos = 0;
    while (true) {
      const sc = findStartCode(data, pos);
      if (!sc) break;
      starts.push(sc);
      pos = sc.index + sc.len;
    }
    if (starts.length < 2) return;

    const lastCompleteEnd = starts[starts.length - 1].index;
    for (let i = 0; i < starts.length - 1; i++) {
      const start = starts[i];
      const next = starts[i + 1];
      const nal = data.subarray(start.index + start.len, next.index);
      if (nal.length === 0) continue;
      const type = nalTypeOf(nal);
      if (type === NAL_SPS) {
        this.sps = nal.slice();
        // New SPS → force reconfigure on next IDR.
        this.configured = false;
        this.seenKey = false;
      } else if (type === NAL_PPS) {
        this.pps = nal.slice();
        this.configured = false;
        this.seenKey = false;
      } else if (type === NAL_IDR || type === NAL_NON_IDR) {
        this._emitAu([nal.slice()], type === NAL_IDR);
      }
      // skip SEI / AUD / others
    }
    this.buffer = data.slice(lastCompleteEnd);
  }
}

/** Paint a VideoFrame onto a canvas, letterboxed; resize backing store to frame. */
export function paintVideoFrame(canvas: HTMLCanvasElement, frame: VideoFrame) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const fw = frame.displayWidth || frame.codedWidth;
  const fh = frame.displayHeight || frame.codedHeight;
  if (!fw || !fh) return;

  // Match canvas buffer to the video so CSS scaling stays sharp.
  if (canvas.width !== fw || canvas.height !== fh) {
    canvas.width = fw;
    canvas.height = fh;
  }
  ctx.drawImage(frame, 0, 0, fw, fh);
}
