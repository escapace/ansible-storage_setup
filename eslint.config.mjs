// @ts-check

import { defineConfig as compose } from 'eslint/config'
import { escapace } from 'oxlint-config-escapace/eslint'

export default compose(escapace(), {
  files: ['**/molecule.yml'],
  rules: {
    'yaml/file-extension': 'off',
  },
})
