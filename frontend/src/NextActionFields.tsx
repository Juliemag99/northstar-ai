import type { NextActionCatalog, NextActionSelection } from './nextAction'

export default function NextActionFields({
  catalog,
  value,
  onChange,
  disabled = false,
  idPrefix,
  optional = true,
}: {
  catalog: NextActionCatalog | null
  value: NextActionSelection
  onChange: (next: NextActionSelection) => void
  disabled?: boolean
  idPrefix: string
  optional?: boolean
}) {
  const choices = catalog?.choices || []
  const selected = choices.find((choice) => choice.code === value.code)
  const needsCustom = Boolean(selected?.requires_detail)

  return (
    <div className="next-action-fields">
      <label className="edit-field">
        <span className="edit-field__label">Next Action</span>
        <select
          id={`${idPrefix}-next-action`}
          className="edit-select"
          value={value.code}
          disabled={disabled || choices.length === 0}
          onChange={(e) => {
            const code = e.target.value
            const choice = choices.find((item) => item.code === code)
            onChange({
              code,
              custom: choice?.requires_detail ? value.custom : '',
            })
          }}
        >
          {optional ? <option value="">Select…</option> : null}
          {choices.map((choice) => (
            <option key={choice.code} value={choice.code}>
              {choice.label}
            </option>
          ))}
        </select>
      </label>
      {needsCustom ? (
        <label className="edit-field">
          <span className="edit-field__label">Custom Next Action</span>
          <input
            id={`${idPrefix}-next-action-custom`}
            className="edit-input"
            value={value.custom}
            disabled={disabled}
            required
            onChange={(e) => onChange({ ...value, custom: e.target.value })}
            placeholder="Required"
          />
        </label>
      ) : null}
    </div>
  )
}
