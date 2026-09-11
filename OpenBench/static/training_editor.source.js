import {EditorState, Compartment} from '@codemirror/state';
import {EditorView, keymap, lineNumbers, highlightActiveLineGutter, drawSelection, highlightActiveLine} from '@codemirror/view';
import {history, historyKeymap, defaultKeymap, indentWithTab} from '@codemirror/commands';
import {search, searchKeymap, highlightSelectionMatches} from '@codemirror/search';
import {bracketMatching, indentOnInput, syntaxHighlighting, HighlightStyle} from '@codemirror/language';
import {rust} from '@codemirror/lang-rust';
import {json} from '@codemirror/lang-json';
import {python} from '@codemirror/lang-python';
import {tags} from '@lezer/highlight';

export function createEditor(textarea, filename, text, onChange, readonly) {
    const language = new Compartment();
    const mode = name => name.endsWith('.rs') ? rust() : name.endsWith('.json') ? json() : name.endsWith('.py') ? python() : [];
    const theme = EditorView.theme({
        '&': {color: 'var(--text)', backgroundColor: 'var(--sidebar)'},
        '.cm-content': {caretColor: 'var(--accent)', fontFamily: 'var(--mono)'},
        '.cm-cursor': {borderLeftColor: 'var(--accent)'},
        '.cm-gutters': {backgroundColor: 'var(--sidebar)', color: 'var(--muted)', borderRight: '1px solid var(--line)'},
        '.cm-activeLineGutter, .cm-activeLine': {backgroundColor: '#ffffff06'},
        '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': {backgroundColor: '#ff575730'},
        '.cm-panels': {backgroundColor: 'var(--surface)', color: 'var(--text)'},
        '.cm-search': {display: 'flex', flexWrap: 'wrap', gap: '4px'},
        '.cm-search input': {maxWidth: '150px'},
        '.cm-textfield, .cm-button': {color: 'var(--text)', background: 'var(--surface-raised)', border: '1px solid var(--line)'},
        '.cm-matchingBracket': {backgroundColor: '#ff575733'},
    }, {dark: true});
    const highlight = HighlightStyle.define([
        {tag: tags.keyword, color: '#ff8585'},
        {tag: [tags.string, tags.special(tags.string)], color: '#b6cea8'},
        {tag: [tags.number, tags.bool], color: '#e5b880'},
        {tag: [tags.typeName, tags.className], color: '#d3b5e9'},
        {tag: tags.comment, color: '#8e8e99'},
        {tag: tags.function(tags.variableName), color: '#9bbfdc'},
    ]);
    const extensions = [
        lineNumbers(), highlightActiveLineGutter(), drawSelection(), highlightActiveLine(), history(),
        search({top: true}), highlightSelectionMatches(), bracketMatching(), indentOnInput(),
        keymap.of([...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
        language.of(mode(filename)), theme, syntaxHighlighting(highlight),
        EditorState.readOnly.of(readonly), EditorView.editable.of(!readonly),
        EditorView.contentAttributes.of({'aria-label': 'Training schedule source code', 'spellcheck': 'false'}),
        EditorView.updateListener.of(update => { if (update.docChanged) onChange(); }),
    ];
    const view = new EditorView({state: EditorState.create({doc: text, extensions})});
    textarea.after(view.dom);
    textarea.hidden = true;
    return {
        getText: () => view.state.doc.toString(),
        getState: () => view.state,
        setFile: (name, value, state) => {
            view.setState(state || EditorState.create({doc: value, extensions}));
            view.dispatch({effects: language.reconfigure(mode(name))});
        },
        focus: () => view.focus(),
    };
}
