import { describe, expect, it } from "vitest";
import { configFields, toConfig } from "./configSchema";

const redisFields = configFields({
  properties: {
    url: { type: "string", default: "redis://localhost:6379/0", "x-url-schemes": ["redis", "rediss", "unix"] },
  },
});

describe("provider URL configuration", () => {
  it.each(["redis://localhost:6379/0", "rediss://user:password@host:6379/0", "unix:///tmp/redis.sock?db=0"])(
    "accepts a declared Redis URL scheme: %s",
    url => {
      expect(toConfig(redisFields, { url })).toEqual({ config: { url }, errors: {} });
    },
  );

  it.each(["http://localhost:6379", "redis://", "redis://host with spaces", "not-a-url"])(
    "rejects an unsupported or incomplete Redis URL: %s",
    url => {
      const result = toConfig(redisFields, { url });
      expect(result.config).toEqual({});
      expect(result.errors.url).toContain("redis:// or rediss:// or unix://");
    },
  );

  it("keeps HTTP endpoint validation for fields without declared schemes", () => {
    const fields = configFields({ properties: { base_url: { type: "string" } } });
    expect(toConfig(fields, { base_url: "redis://localhost:6379" }).errors.base_url).toContain("http:// or https://");
    expect(toConfig(fields, { base_url: "https://localhost:9000/v1" }).errors).toEqual({});
  });
});
