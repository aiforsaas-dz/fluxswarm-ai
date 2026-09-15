export const ConciseReplies = async () => {
  const RULE = [
    "CONCISE-RESPONSE RULE (enforced every turn):",
    "- End each turn with 5 lines or fewer: what was done / result / blockers.",
    "- No preamble, no recap, no rationale, no 'here is what I will do'.",
    "- If the user explicitly asked for a verbose deliverable (proposal, analysis, prompt, prompt text), output exactly that deliverable and nothing more.",
    "- Execution orders: run the tools first, then reply with the short summary.",
  ].join("\n")

  return {
    "experimental.chat.system.transform": async (_input, output) => {
      output.system.push(RULE)
    },
    "chat.params": async (_input, output) => {
      if (output.maxOutputTokens === undefined || output.maxOutputTokens > 8192) {
        output.maxOutputTokens = 8192
      }
    },
  }
}