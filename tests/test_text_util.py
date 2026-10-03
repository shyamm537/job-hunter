from src.ingestion.text_util import html_to_text


def test_none_and_empty_give_empty_string():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""
    assert html_to_text("   ") == ""


def test_plain_text_passes_through():
    assert html_to_text("Build data pipelines.") == "Build data pipelines."


def test_paragraphs_are_separated_by_a_blank_line():
    assert html_to_text("<p>First.</p><p>Second.</p>") == "First.\n\nSecond."


def test_line_breaks():
    assert html_to_text("one<br>two<br/>three") == "one\ntwo\nthree"


def test_list_items_get_dashes_without_blank_lines_between():
    html = "<p>You will:</p><ul><li>Build models</li><li>Write SQL</li></ul><p>Apply now.</p>"
    assert html_to_text(html) == "You will:\n\n- Build models\n- Write SQL\n\nApply now."


def test_nested_tags_and_inline_spacing_are_kept():
    html = "<p>Use <b>Python</b> and <i>R</i>, <a href='x'>not</a> Excel.</p>"
    assert html_to_text(html) == "Use Python and R, not Excel."


def test_headings_stand_on_their_own_lines():
    assert html_to_text("<h2>About us</h2><p>We hire.</p>") == "About us\n\nWe hire."


def test_entities_are_decoded():
    assert html_to_text("<p>R&amp;D &lt;team&gt; &quot;hi&quot; caf&eacute;</p>") == 'R&D <team> "hi" café'
    # A non-breaking space is whitespace, not a stray character.
    assert html_to_text("a&nbsp;&nbsp;b") == "a b"
    assert html_to_text("&#8211; dash") == "– dash"


def test_script_and_style_content_is_dropped():
    html = "<style>p {color: red}</style><p>Hello</p><script>alert('x')</script>"
    assert html_to_text(html) == "Hello"


def test_source_formatting_whitespace_collapses():
    html = "<p>\n   Lots   of\n\t space\n</p>\n\n\n<p>Next</p>"
    assert html_to_text(html) == "Lots of space\n\nNext"


def test_many_empty_blocks_collapse_to_one_blank_line():
    assert html_to_text("<p>A</p><p></p><p><br></p><p>B</p>") == "A\n\nB"


def test_malformed_html_does_not_raise():
    assert html_to_text("<p>Unclosed <b>bold <i>italic") == "Unclosed bold italic"
    # A stray closing block tag is still a block boundary.
    assert html_to_text("<p>Stray </div> close</p>") == "Stray\n\nclose"
    # A bare "<" or ">" that is not a tag stays as text.
    assert html_to_text("1 < 2 and 3 > 2") == "1 < 2 and 3 > 2"
    assert html_to_text("<<<>>>") == "<<<>>>"
    # An unterminated tag at the very end is dropped rather than raising.
    assert html_to_text("<p>x</p") == "x"


def test_non_string_input_does_not_raise():
    assert html_to_text(12345) == "12345"
