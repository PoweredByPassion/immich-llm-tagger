/** Tailwind CLI 构建 配置：扫描模板里的 class。 */
module.exports = {
  content: ["app/web/templates/**/*.html", "app/web/static/input.css"],
  darkMode: "class",
  safelist: ["num", "btn", "btn-primary", "btn-secondary", "btn-danger", "btn-ghost", "input", "card", "pill"],
  theme: {
    extend: {
      colors: {
        void: "#090a0f",
        canvas: "#0d0f14",
        surface: {
          1: "#13151c",
          2: "#181b24",
          3: "#202430",
        },
        border: {
          subtle: "#20232d",
          muted: "#282c39",
          focus: "#5e6ad2",
        },
        linear: {
          accent: "#5e6ad2",
          hover: "#6f7be8",
          active: "#4f5ab8",
          subtle: "rgba(94, 106, 210, 0.12)",
          border: "rgba(94, 106, 210, 0.35)",
        },
      },
      borderRadius: {
        DEFAULT: "var(--radius, 6px)",
      },
    },
  },
};
