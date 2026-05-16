import hljs from 'highlight.js/lib/core'
import json from 'highlight.js/lib/languages/json'
import 'highlight.js/styles/atom-one-dark.css'

hljs.registerLanguage('json', json)

export const highlightConfig = {
  hljs
}
