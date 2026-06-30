import { useEffect, useRef, useState, type SyntheticEvent } from 'react'
import { useLocale } from '@/contexts/LocaleContext'
import { listBenchmarks } from '@/api/eval'
import Button from '@/components/ui/Button'
import Card from '@/components/ui/Card'
import FormField from '@/components/ui/FormField'
import { FORM_INPUT_CLASS, FORM_LABEL_CLASS, inputClass } from '@/components/ui/formStyles'
import { ChevronDown, ChevronUp, Stethoscope } from 'lucide-react'

type RcaAgentMode = 'mock' | 'http' | 'openai'

const RCA_DATASET = 'rcaeval_rca'
const RCA_DUMMY_API_URL = 'http://127.0.0.1/unused'

interface Props {
  onSubmit: (config: Record<string, unknown>) => void
  disabled?: boolean
  initialDataset?: string
}

export default function EvalConfigForm({ onSubmit, disabled, initialDataset }: Props) {
  const { t } = useLocale()
  const [rcaPreset, setRcaPreset] = useState(initialDataset === RCA_DATASET)
  const [model, setModel] = useState('')
  const [apiUrl, setApiUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [datasets, setDatasets] = useState(initialDataset ?? '')
  const [limit, setLimit] = useState('5')
  const [evalBatchSize, setEvalBatchSize] = useState('16')
  const [showMore, setShowMore] = useState(false)
  const [repeats, setRepeats] = useState('1')
  const [timeout, setTimeout_] = useState('60')
  const [stream, setStream] = useState(false)
  const [temperature, setTemperature] = useState('')
  const [topP, setTopP] = useState('')
  const [maxTokens, setMaxTokens] = useState('')
  const [topK, setTopK] = useState('')
  const [datasetArgs, setDatasetArgs] = useState('')
  const [rcaAgentMode, setRcaAgentMode] = useState<RcaAgentMode>('mock')
  const [rcaAgentUrl, setRcaAgentUrl] = useState('')
  const [rcaOpenaiApiUrl, setRcaOpenaiApiUrl] = useState('https://api.openai.com/v1')
  const [rcaOpenaiApiKey, setRcaOpenaiApiKey] = useState('')
  const [rcaOpenaiModel, setRcaOpenaiModel] = useState('gpt-4.1-mini')
  const [rcaMetricsLimit, setRcaMetricsLimit] = useState('80')
  const [rcaTimeout, setRcaTimeout] = useState('120')

  // Validation
  const [errors, setErrors] = useState<Record<string, string>>({})

  // Dataset autocomplete
  const [benchmarkNames, setBenchmarkNames] = useState<string[]>([])
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [filteredSuggestions, setFilteredSuggestions] = useState<string[]>([])
  const datasetInputRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (initialDataset) setDatasets(initialDataset)
    if (initialDataset === RCA_DATASET) applyRcaPreset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialDataset])

  const buildRcaDatasetArgs = () => ({
    [RCA_DATASET]: {
      extra_params: {
        agent_mode: rcaAgentMode,
        agent_url: rcaAgentUrl,
        openai_api_url: rcaOpenaiApiUrl,
        openai_api_key: rcaOpenaiApiKey,
        openai_model: rcaOpenaiModel,
        metrics_limit: Number(rcaMetricsLimit || 80),
        timeout: Number(rcaTimeout || 120),
      },
    },
  })

  const applyRcaPreset = () => {
    setRcaPreset(true)
    setModel(rcaAgentMode === 'openai' ? rcaOpenaiModel : 'rca-agent')
    setApiUrl(RCA_DUMMY_API_URL)
    setApiKey('')
    setDatasets(RCA_DATASET)
    setLimit('')
    setEvalBatchSize('1')
    setTimeout_(rcaTimeout)
    setStream(false)
    setDatasetArgs(JSON.stringify(buildRcaDatasetArgs(), null, 2))
    setErrors({})
  }

  useEffect(() => {
    if (!rcaPreset) return
    setModel(rcaAgentMode === 'openai' ? rcaOpenaiModel : 'rca-agent')
    setApiUrl(RCA_DUMMY_API_URL)
    setDatasets(RCA_DATASET)
    setEvalBatchSize('1')
    setTimeout_(rcaTimeout)
    setDatasetArgs(JSON.stringify(buildRcaDatasetArgs(), null, 2))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rcaPreset, rcaAgentMode, rcaAgentUrl, rcaOpenaiApiUrl, rcaOpenaiApiKey, rcaOpenaiModel, rcaMetricsLimit, rcaTimeout])

  useEffect(() => {
    listBenchmarks()
      .then((res) => {
        const names = [
          ...(res.text ?? []).map((b) => b.name),
          ...(res.multimodal ?? []).map((b) => b.name),
        ]
        setBenchmarkNames(names)
      })
      .catch(() => {})
  }, [])

  // Close suggestions on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (datasetInputRef.current && !datasetInputRef.current.contains(e.target as Node)) {
        setShowSuggestions(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const handleDatasetChange = (val: string) => {
    setDatasets(val)
    // Filter based on last token after comma
    const parts = val.split(',')
    const current = parts[parts.length - 1].trim().toLowerCase()
    if (current) {
      const matches = benchmarkNames.filter((n) => n.toLowerCase().includes(current))
      setFilteredSuggestions(matches.slice(0, 8))
      setShowSuggestions(matches.length > 0)
    } else {
      setShowSuggestions(false)
    }
    if (errors.datasets) setErrors((prev) => ({ ...prev, datasets: '' }))
  }

  const selectSuggestion = (name: string) => {
    const parts = datasets.split(',').map((s) => s.trim())
    parts[parts.length - 1] = name
    setDatasets(parts.join(', '))
    setShowSuggestions(false)
  }

  const handleSubmit = (e: SyntheticEvent<HTMLFormElement>) => {
    e.preventDefault()
    const newErrors: Record<string, string> = {}
    if (!model.trim()) newErrors.model = 'Required'
    if (!datasets.trim()) newErrors.datasets = 'Required'
    if (Object.keys(newErrors).length > 0) {
      setErrors(newErrors)
      return
    }
    setErrors({})

    const config: Record<string, unknown> = {
      model,
      datasets: datasets.split(',').map((s) => s.trim()).filter(Boolean),
      limit: limit ? Number(limit) : undefined,
      eval_batch_size: evalBatchSize ? Number(evalBatchSize) : undefined,
    }
    if (rcaPreset) {
      config.model_id = rcaAgentMode === 'mock' ? 'web-rca-mock-agent' : `web-rca-${rcaAgentMode}-agent`
      config.eval_type = 'mock_llm'
      config.api_url = apiUrl || RCA_DUMMY_API_URL
      config.collect_perf = false
    }
    if (apiUrl) config.api_url = apiUrl
    if (apiKey) config.api_key = apiKey
    if (repeats && Number(repeats) > 1) config.repeats = Number(repeats)
    if (timeout) config.timeout = Number(timeout)
    if (stream) config.stream = true
    // Wrap generation params into generation_config dict
    const genConfig: Record<string, unknown> = {}
    if (temperature) genConfig.temperature = Number(temperature)
    if (topP) genConfig.top_p = Number(topP)
    if (maxTokens) genConfig.max_tokens = Number(maxTokens)
    if (topK) genConfig.top_k = Number(topK)
    if (Object.keys(genConfig).length > 0) config.generation_config = genConfig
    if (datasetArgs) {
      try {
        config.dataset_args = JSON.parse(datasetArgs)
      } catch { /* ignore invalid JSON */ }
    }
    onSubmit(config)
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <section className="flex flex-col gap-4 border-b border-[var(--border)] pb-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-sm)] bg-[var(--accent-dim)] text-[var(--accent)]">
              <Stethoscope size={16} />
            </div>
            <div>
              <div className="text-sm font-semibold text-[var(--text)]">RCAEval RCA</div>
              <div className="text-xs text-[var(--text-muted)]">Offline root-cause-analysis agent benchmark</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant={rcaPreset ? 'primary' : 'outline'}
              size="sm"
              onClick={applyRcaPreset}
            >
              Use RCA preset
            </Button>
            {rcaPreset && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setRcaPreset(false)}
              >
                Standard mode
              </Button>
            )}
          </div>
        </div>

        {rcaPreset && (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <FormField label="Agent Mode">
              <select
                value={rcaAgentMode}
                onChange={(e) => setRcaAgentMode(e.target.value as RcaAgentMode)}
                className={FORM_INPUT_CLASS}
              >
                <option value="mock">Mock</option>
                <option value="http">HTTP Agent</option>
                <option value="openai">OpenAI Compatible</option>
              </select>
            </FormField>
            {rcaAgentMode === 'http' && (
              <FormField label="Agent URL" className="md:col-span-2">
                <input
                  value={rcaAgentUrl}
                  onChange={(e) => setRcaAgentUrl(e.target.value)}
                  className={FORM_INPUT_CLASS}
                  placeholder="http://127.0.0.1:7000/api/v1/diagnose"
                />
              </FormField>
            )}
            {rcaAgentMode === 'openai' && (
              <>
                <FormField label="OpenAI API URL">
                  <input value={rcaOpenaiApiUrl} onChange={(e) => setRcaOpenaiApiUrl(e.target.value)} className={FORM_INPUT_CLASS} />
                </FormField>
                <FormField label="OpenAI Model">
                  <input value={rcaOpenaiModel} onChange={(e) => setRcaOpenaiModel(e.target.value)} className={FORM_INPUT_CLASS} />
                </FormField>
                <FormField label="OpenAI API Key">
                  <input type="password" value={rcaOpenaiApiKey} onChange={(e) => setRcaOpenaiApiKey(e.target.value)} className={FORM_INPUT_CLASS} />
                </FormField>
              </>
            )}
            <FormField label="Metrics Limit">
              <input type="number" value={rcaMetricsLimit} onChange={(e) => setRcaMetricsLimit(e.target.value)} className={FORM_INPUT_CLASS} />
            </FormField>
            <FormField label="Agent Timeout">
              <input type="number" value={rcaTimeout} onChange={(e) => setRcaTimeout(e.target.value)} className={FORM_INPUT_CLASS} />
            </FormField>
          </div>
        )}
      </section>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <FormField label={t('eval.modelName')} required error={errors.model}>
          <input
            value={model}
            onChange={(e) => { setModel(e.target.value); if (errors.model) setErrors((p) => ({ ...p, model: '' })) }}
            className={inputClass(errors.model)}
            placeholder="Qwen/Qwen2.5-0.5B-Instruct"
          />
        </FormField>

        {/* Datasets with autocomplete */}
        <FormField label={t('eval.datasets')} required error={errors.datasets} className="relative">
          <div ref={datasetInputRef}>
            <input
              value={datasets}
              onChange={(e) => handleDatasetChange(e.target.value)}
              onFocus={() => { if (filteredSuggestions.length) setShowSuggestions(true) }}
              className={inputClass(errors.datasets)}
              placeholder="gsm8k, arc"
            />
            {showSuggestions && (
              <div className="absolute z-50 left-0 right-0 mt-1 rounded-[var(--radius-sm)] border border-[var(--border-md)] bg-[var(--bg-card)] shadow-[var(--shadow)] overflow-hidden max-h-48 overflow-y-auto">
                {filteredSuggestions.map((name) => (
                  <button
                    key={name}
                    type="button"
                    onClick={() => selectSuggestion(name)}
                    className="w-full text-left px-3 py-2 text-sm text-[var(--text)] hover:bg-[var(--bg-card2)] transition-colors cursor-pointer"
                  >
                    {name}
                  </button>
                ))}
              </div>
            )}
          </div>
        </FormField>

        <FormField label={t('eval.apiUrl')}>
          <input value={apiUrl} onChange={(e) => setApiUrl(e.target.value)} className={FORM_INPUT_CLASS} placeholder="http://localhost:8000/v1" />
        </FormField>

        <FormField label={t('eval.apiKey')}>
          <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} className={FORM_INPUT_CLASS} placeholder="sk-..." />
        </FormField>

        <FormField label={t('eval.limit')}>
          <input type="number" value={limit} onChange={(e) => setLimit(e.target.value)} className={FORM_INPUT_CLASS} />
        </FormField>

        <FormField label={t('eval.batchSize')}>
          <input type="number" value={evalBatchSize} onChange={(e) => setEvalBatchSize(e.target.value)} className={FORM_INPUT_CLASS} />
        </FormField>
      </div>

      {/* More params toggle */}
      <button
        type="button"
        onClick={() => setShowMore(!showMore)}
        className="flex items-center gap-1 text-xs text-[var(--accent)] hover:underline cursor-pointer"
      >
        {t('eval.moreParams')}
        {showMore ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>

      {showMore && (
        <Card className="!p-0">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 p-4">
            <FormField label={t('eval.repeats')}>
              <input type="number" value={repeats} onChange={(e) => setRepeats(e.target.value)} className={FORM_INPUT_CLASS} />
            </FormField>
            <FormField label={t('eval.timeout')}>
              <input type="number" value={timeout} onChange={(e) => setTimeout_(e.target.value)} className={FORM_INPUT_CLASS} />
            </FormField>
            <div className="flex items-end gap-2 pb-0.5">
              <label className="flex items-center gap-1.5 text-xs text-[var(--text-muted)] cursor-pointer">
                <input type="checkbox" checked={stream} onChange={(e) => setStream(e.target.checked)} className="accent-[var(--accent)]" />
                {t('eval.stream')}
              </label>
            </div>
            <FormField label={t('eval.temperature')}>
              <input type="number" step="0.1" value={temperature} onChange={(e) => setTemperature(e.target.value)} className={FORM_INPUT_CLASS} />
            </FormField>
            <FormField label={t('eval.topP')}>
              <input type="number" step="0.1" value={topP} onChange={(e) => setTopP(e.target.value)} className={FORM_INPUT_CLASS} />
            </FormField>
            <FormField label={t('eval.maxTokens')}>
              <input type="number" value={maxTokens} onChange={(e) => setMaxTokens(e.target.value)} className={FORM_INPUT_CLASS} />
            </FormField>
            <FormField label={t('eval.topK')}>
              <input type="number" value={topK} onChange={(e) => setTopK(e.target.value)} className={FORM_INPUT_CLASS} />
            </FormField>
            <div className="md:col-span-2">
              <label className={FORM_LABEL_CLASS}>{t('eval.datasetArgs')}</label>
              <textarea
                value={datasetArgs}
                onChange={(e) => setDatasetArgs(e.target.value)}
                className={`${FORM_INPUT_CLASS} h-20 resize-y`}
                style={{ fontFamily: 'var(--font-mono)' }}
                placeholder='{"gsm8k": {"few_shot_num": 4}}'
              />
            </div>
          </div>
        </Card>
      )}

      <Button type="submit" variant="primary" disabled={disabled} className="btn-glow">
        {t('eval.startEval')}
      </Button>
    </form>
  )
}
