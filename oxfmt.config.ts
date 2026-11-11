import { defineConfig } from 'oxfmt'

export default defineConfig({
  arrowParens: 'always',
  bracketSameLine: false,
  bracketSpacing: true,
  htmlWhitespaceSensitivity: 'strict',
  ignorePatterns: ['.cache', '.firecrawl', 'coverage', 'lib', 'pnpm-lock.yaml'],
  printWidth: 100,
  quoteProps: 'consistent',
  semi: false,
  singleQuote: true,
  // syncpack owns package.json ordering.
  sortPackageJson: false,
  tabWidth: 2,
  trailingComma: 'all',
  overrides: [
    {
      files: [
        'defaults/**/*.{yml,yaml}',
        'handlers/**/*.{yml,yaml}',
        'meta/**/*.{yml,yaml}',
        'molecule/**/*.{yml,yaml}',
        'tasks/**/*.{yml,yaml}',
        'vars/**/*.{yml,yaml}',
      ],
      options: { singleQuote: false },
    },
  ],
})
