/** Edge coverage for `lib/mySongs.ts` (issue #37): corrupt/unavailable storage never throws. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { MY_SONGS_KEY, addMySong, hasMySong, loadMySongs } from "./mySongs";

const memoryStorage = (data: Record<string, string> = {}) => ({
  data,
  getItem: (k: string) => (k in data ? data[k] : null),
  setItem: (k: string, v: string) => { data[k] = String(v); },
});

afterEach(() => vi.unstubAllGlobals());

describe("loadMySongs with corrupt storage", () => {
  it("unparseable JSON loads as an empty set", () => {
    vi.stubGlobal("localStorage", memoryStorage({ [MY_SONGS_KEY]: "{not json" }));
    expect(loadMySongs().size).toBe(0);
  });

  it("a JSON object (non-array) loads as an empty set", () => {
    vi.stubGlobal("localStorage", memoryStorage({ [MY_SONGS_KEY]: '{"a":1}' }));
    expect(loadMySongs().size).toBe(0);
  });

  it("a JSON string loads as an empty set", () => {
    vi.stubGlobal("localStorage", memoryStorage({ [MY_SONGS_KEY]: '"s1"' }));
    expect(loadMySongs().size).toBe(0);
  });

  it("non-string array entries are dropped and string ids kept", () => {
    vi.stubGlobal("localStorage", memoryStorage({ [MY_SONGS_KEY]: '["s1", 2, null, "s3"]' }));
    expect([...loadMySongs()]).toEqual(["s1", "s3"]);
  });

  it("addMySong over corrupt data recovers with just the new id", () => {
    vi.stubGlobal("localStorage", memoryStorage({ [MY_SONGS_KEY]: "garbage" }));
    expect([...addMySong("s1")]).toEqual(["s1"]);
  });
});

describe("mySongs when storage misbehaves", () => {
  it("getItem throwing loads as an empty set", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => { throw new Error("denied"); },
      setItem: () => undefined,
    });
    expect(loadMySongs().size).toBe(0);
  });

  it("setItem throwing still returns the set including the new id", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => null,
      setItem: () => { throw new Error("quota"); },
    });
    expect(hasMySong(addMySong("s1"), "s1")).toBe(true);
  });

  it("accessing localStorage itself throwing degrades to in-memory", () => {
    const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() { throw new Error("SecurityError"); },
    });
    try {
      expect(loadMySongs().size).toBe(0);
      expect(hasMySong(addMySong("s1"), "s1")).toBe(true);
    } finally {
      if (original) Object.defineProperty(globalThis, "localStorage", original);
      else delete (globalThis as Record<string, unknown>).localStorage;
    }
  });

  it("an absent localStorage loads as an empty set", () => {
    vi.stubGlobal("localStorage", undefined);
    expect(loadMySongs().size).toBe(0);
  });
});

describe("mySongs cap and lookup", () => {
  it("keeps at most 200 ids, dropping the oldest", () => {
    const backing = memoryStorage();
    vi.stubGlobal("localStorage", backing);
    for (let i = 0; i < 205; i++) addMySong(`s${i}`);
    const stored = loadMySongs();
    expect(stored.size).toBe(200);
    expect(stored.has("s0")).toBe(false);
    expect(stored.has("s204")).toBe(true);
  });

  it("re-adding an old id marks it most recent so it survives the cap", () => {
    vi.stubGlobal("localStorage", memoryStorage());
    for (let i = 0; i < 200; i++) addMySong(`s${i}`);
    addMySong("s0");
    addMySong("new");
    const stored = loadMySongs();
    expect(stored.has("s0")).toBe(true);
    expect(stored.has("s1")).toBe(false);
  });

  it("hasMySong on an empty set is false", () => {
    expect(hasMySong(new Set(), "s1")).toBe(false);
  });

  it("hasMySong is false for a null or undefined id", () => {
    const set = new Set(["s1"]);
    expect(hasMySong(set, null)).toBe(false);
    expect(hasMySong(set, undefined)).toBe(false);
  });
});
