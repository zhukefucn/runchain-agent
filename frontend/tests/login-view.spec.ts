import { fireEvent, render, screen } from "@testing-library/vue";
import { expect, it } from "vitest";

import { createTestingApp } from "./test-app";

it("uses the approved 润辰科技 brand instead of the RunChain wordmark", async () => {
  const { router, pinia, App } = createTestingApp({ username: "manager0001", role: "manager" });
  await router.isReady();
  render(App, { global: { plugins: [pinia, router] } });

  expect(screen.getByRole("img", { name: "润辰科技" })).toBeInTheDocument();
  expect(screen.queryByText("RunChain")).not.toBeInTheDocument();
});

it("fills the exact manager demo credentials without relying on browser autofill", async () => {
  const { router, pinia, App } = createTestingApp({ username: "manager0001", role: "manager" });
  await router.isReady();
  render(App, { global: { plugins: [pinia, router] } });

  await fireEvent.click(screen.getByRole("button", { name: "填入 manager0001 演示账号" }));

  expect(screen.getByLabelText("用户名")).toHaveValue("manager0001");
  expect(screen.getByLabelText("密码")).toHaveValue("12345678");
});
