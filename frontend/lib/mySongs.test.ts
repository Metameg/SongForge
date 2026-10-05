import { afterEach, describe, expect, it, vi } from "vitest";
import { addMySong, hasMySong, loadMySongs } from "./mySongs";

/** In-memory Storage double; `data` is the persistent backing store across "reloads". */
const memoryStorage = (data: Record<string, string> = {}) => ({
  data,
  getItem: (k: string) => (k in data ? data[k] : null),
  setItem: (k: string, v: string) => { data[k] = String(v); },
  removeItem: (k: string) => { delete data[k]; },
});

afterEach(() => vi.unstubAllGlobals());

describe("mySongs with a working localStorage", () => {
  it("starts empty", () => {
    vi.stubGlobal("localStorage", memoryStorage());
    expect(loadMySongs().size).toBe(0);
  });
  it("addMySong returns a set containing the id", () => {
    vi.stubGlobal("localStorage", memoryStorage());
    expect(hasMySong(addMySong("s1"), "s1")).toBe(true);
  });
  it("hasMySong is false for an id never added", () => {
    vi.stubGlobal("localStorage", memoryStorage());
    expect(hasMySong(addMySong("s1"), "s2")).toBe(false);
  });
  it("persists across a reload (re-load from the same backing store)", () => {
    const backing = memoryStorage();
    vi.stubGlobal("localStorage", backing);
    addMySong("s1");
    addMySong("s2");
    vi.stubGlobal("localStorage", memoryStorage(backing.data)); // fresh page, same store
    const reloaded = loadMySongs();
    expect(hasMySong(reloaded, "s1")).toBe(true);
    expect(hasMySong(reloaded, "s2")).toBe(true);
  });
  it("adding the same id twice keeps a single entry", () => {
    vi.stubGlobal("localStorage", memoryStorage());
    addMySong("s1");
    expect(addMySong("s1").size).toBe(1);
  });
  it("tolerates corrupt stored data by loading an empty set", () => {
    vi.stubGlobal("localStorage", memoryStorage({ "songforge:mySongs": "{not json" }));
    expect(() => loadMySongs()).not.toThrow();
    expect(loadMySongs().size).toBe(0);
  });
});

describe("mySongs when localStorage is unavailable", () => {
  const throwingStorage = () => ({
    getItem: () => { throw new Error("denied"); },
    setItem: () => { throw new Error("denied"); },
    removeItem: () => { throw new Error("denied"); },
  });

  it("a throwing localStorage never throws on load", () => {
    vi.stubGlobal("localStorage", throwingStorage());
    expect(() => loadMySongs()).not.toThrow();
    expect(loadMySongs().size).toBe(0);
  });
  it("a throwing localStorage degrades to an in-memory set on add", () => {
    vi.stubGlobal("localStorage", throwingStorage());
    let set!: Set<string>;
    expect(() => { set = addMySong("s1"); }).not.toThrow();
    expect(hasMySong(set, "s1")).toBe(true);
  });
  it("a throwing localStorage getter on globalThis never throws", () => {
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() { throw new Error("SecurityError"); },
    });
    try {
      expect(() => loadMySongs()).not.toThrow();
      expect(() => addMySong("s1")).not.toThrow();
    } finally {
      delete (globalThis as { localStorage?: unknown }).localStorage;
    }
  });
  it("an absent localStorage never throws and still tracks in memory", () => {
    vi.stubGlobal("localStorage", undefined);
    expect(() => loadMySongs()).not.toThrow();
    expect(hasMySong(addMySong("s1"), "s1")).toBe(true);
  });
});
