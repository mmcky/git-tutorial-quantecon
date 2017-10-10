# -*- coding: utf-8 -*-
"""
    sphinx.ext.jupyter
    ~~~~~~~~~~~~~~~~~~

    
"""

from enum import Enum
from xml.etree.ElementTree import ElementTree
import os.path
import codecs
import datetime
import xml.sax.saxutils
import nbformat
from nbconvert.preprocessors import ExecutePreprocessor
import docutils
from sphinx.builders import Builder
from sphinx.util.osutil import ensuredir, os_path, relative_uri, movefile, copyfile
from sphinx.util.console import bold, darkgreen, brown
from sphinx.util.matching import Matcher
from sphinx.util.fileutil import copy_asset
import re


"""
Simple extensible translator for programming language names between Sphinx
and Jupyter.

These two file formats accept ever-so-slightly different sets of programming
language names; this class provides a friendly mechanism for translation between
the two.

The data itself is stored in an XML file within the templates directory configured
in conf.py; deciding whether this is the most appropriate place to store that
information is a @todo

By default, if there is no entry in the XML file for a given language, the translator
will return the language it was given; this decision was predicated on the fact that
the vast majority of languages do not need any translation between Sphinx and Jupyter.

"""
class LanguageTranslator:
    def __init__(self, templatePaths):
        self.translator = dict()

        sourceFile = "languages.xml"
        for potentialPath in templatePaths:
            fullFilename = os.path.normpath(potentialPath + "/" + sourceFile)
            if os.path.isfile(fullFilename):
                xmlParser = ElementTree()
                xmlRoot = xmlParser.parse(fullFilename)

                languages = xmlRoot.findall("language")
                for language in languages:
                    sphinxLang = None
                    jupyterLang = None

                    for child in language:
                        if child.tag == "sphinx-name":
                            sphinxLang = child.text
                        elif child.tag == "jupyter-name":
                            jupyterLang = child.text

                    if sphinxLang and jupyterLang:
                        self.translator[sphinxLang] = jupyterLang
                    else:
                        # Explicit silent failure; ignore malformed data.
                        pass


    """
    Translates the provided language name, if it is found in the language dictionary.

    If the language is not found in the dictionary, return the name that was given.
    """
    def translate(self, sphinxLanguageName):
        return self.translator[sphinxLanguageName] if sphinxLanguageName in self.translator else sphinxLanguageName


class JupyterOutputCellGenerators(Enum):
    CODE        = 1
    MARKDOWN    = 2
    CODE_OUTPUT = 3


    """
    Infers the type of output cell to be generated from the class attributes in the original Sphinx cell.

    Note that there is no guarantee as to the ordering or priority of output classes; a cell with the
    attribute ":class: no-execute output" is not considered to be well-defined.
    """
    @staticmethod
    def GetGeneratorFromClasses(class_list):
        res = JupyterOutputCellGenerators.CODE

        for item in class_list:
            if item == "no-execute":
                res = JupyterOutputCellGenerators.MARKDOWN
            elif item == "skip-test":
                res = JupyterOutputCellGenerators.MARKDOWN
            elif item =="output":
                res = JupyterOutputCellGenerators.CODE_OUTPUT

        return res


    """
    Generates the Jupyter cell object.
    """
    def Generate(self, formatted_text, translator):
        res = None
        if self is JupyterOutputCellGenerators.CODE:
            res = nbformat.v4.new_code_cell(formatted_text)
        elif self is JupyterOutputCellGenerators.CODE_OUTPUT:
            res = nbformat.v4.new_output(output_type="stream", text=formatted_text)
        elif self is JupyterOutputCellGenerators.MARKDOWN:
            # Add triple backticks and the name of the language to the code block,
            # so that Jupyter renders the markdown correctly.
            language = translator.nodelang if translator.nodelang else ""
            raw_markdown = "```" + language + "\n" + formatted_text + "\n```\n"
            res = nbformat.v4.new_markdown_cell(raw_markdown)
        else:
            raise Exception("Invalid output cell type passed to JupyterOutputCellGenerator.Generate.")

        return res


class JupyterWriter(docutils.writers.Writer):
    def __init__(self, builder):
        docutils.writers.Writer.__init__(self)
        self.builder = builder

        if "jupyter_conversion_mode" not in builder.config \
            or builder.config["jupyter_conversion_mode"] is None:
            self.warn(
                "jupyter_conversion_mode is not given in conf.py. "
                "Set conversion_mode as default(code)")
            self.conversion_mode = "code"
        else:
            if builder.config["jupyter_conversion_mode"] == "code":
                self.conversion_mode = "code"
            elif builder.config["jupyter_conversion_mode"] == "all":
                self.conversion_mode = "all"
            else:
                self.warn(
                    "Invalid jupyter_conversion_mode is given({}). "
                    "Set conversion_mode as default(code)"
                    .format(builder.config["jupyter_conversion_mode"]))
                self.conversion_mode = "code"

        if self.conversion_mode == "code":
            self.translator_class = JupyterCodeTranslator
        else:
            self.translator_class = JupyterTranslator

    def translate(self):
        self.document.settings.newlines = \
            self.document.settings.indents = \
            self.builder.env.config.xml_pretty

        self.visitor = visitor = self.translator_class(
            self.builder,
            self.document)

        self.document.walkabout(visitor)
        self.output = nbformat.writes(visitor.output)


class JupyterCodeTranslator(docutils.nodes.GenericNodeVisitor):

    URI_SPACE_REPLACE_FROM = re.compile(r"\s")
    URI_SPACE_REPLACE_TO = "-"

    def __init__(self, builder, document):
        docutils.nodes.NodeVisitor.__init__(self, document)

        self.lang     = None
        self.nodelang = None

        self.langTranslator = LanguageTranslator(builder.config["templates_path"])

        # Reporter
        self.warn = self.document.reporter.warning
        self.error = self.document.reporter.error

        # Settings
        self.settings = settings = document.settings
        self.builder = builder
        self.source_file_name = get_source_file_name(
            self.settings._source, 
            self.settings.env.srcdir)
        self.default_lang = "python3"
        
        # Create output notebook
        self.output = nbformat.v4.new_notebook()

        # Variables defined in conf.py
        self.jupyter_kernels = builder.config["jupyter_kernels"]
        self.jupyter_headers = builder.config["jupyter_headers"]
        self.jupyter_write_metadata = builder.config["jupyter_write_metadata"]

        # Welcome message block
        template_paths = builder.config["templates_path"]
        welcome_block_filename = builder.config["jupyter_welcome_block"]

        full_path_to_welcome = None
        for template_path in template_paths:
            if os.path.isfile(template_path + "/" + welcome_block_filename):
                full_path_to_welcome = os.path.normpath(template_path + "/" + welcome_block_filename)

        if full_path_to_welcome:
            with open(full_path_to_welcome) as input_file:
                lines = input_file.readlines()

            line_text = "".join(lines)
            formatted_line_text = self.strip_blank_lines_in_end_of_block(line_text)
            nb_header_block = nbformat.v4.new_markdown_cell(formatted_line_text)

            # Add the welcome block to the output stream straight away
            self.output["cells"].append(nb_header_block)

        # Write metadata
        if self.jupyter_write_metadata:
            meta_text = \
                "Notebook created: {:%Y-%m-%d %H:%M:%S}  \n"\
                "Generated from: {}  "

            metadata = meta_text.format(
                datetime.datetime.now(),
                self.source_file_name)

            self.output["cells"].append(nbformat.v4.new_markdown_cell(metadata))

        # Variables used in visit/depart
        self.in_code_block = False # if False, it means in markdown_cell
        self.output_cell_type = None
        self.code_lines = []

    # generic visit and depart methods
    # --------------------------------
    simple_nodes = (
        docutils.nodes.TextElement,
        docutils.nodes.image, 
        docutils.nodes.colspec, 
        docutils.nodes.transition) # empty elements

    def default_visit(self, node):
        pass

    def default_departure(self, node):
        pass

    # specific visit and depart methods
    # ---------------------------------

    # ==============
    #  Sections
    # ==============
    def visit_document(self, node):
        """at start
        """
        # we need to give the translator a default language!
        # the translator needs to know what language the document is written in
        # before depart_document is called.
        self.lang = self.default_lang



    def depart_document(self, node):
        """at end
        """
        if not self.lang:
            self.warn(
                "Highlighting language is not given in .rst file. "
                "Set kernel as default(python3)")
            self.lang = self.default_lang

        # Header(insert after metadata)
        if self.jupyter_headers is not None:
            try:
                for h in self.jupyter_headers[self.lang][::-1]:
                    if self.jupyter_write_metadata:
                        self.output["cells"].insert(1, h)
                    else:
                        self.output["cells"].insert(0, h)
            except:
                self.warn(
                    "Invalid jupyter headers. "
                    "jupyter_headers: {}, lang: {}"
                    .format(self.jupyter_headers, self.lang))

        # Update metadata
        if self.jupyter_kernels is not None:
            try:
                self.output.metadata.kernelspec = \
                    self.jupyter_kernels[self.lang]["kernelspec"]
            except:
                self.warn(
                    "Invalid jupyter kernels. "
                    "jupyter_kernels: {}, lang: {}"
                    .format(self.jupyter_kernels, self.lang))

    def visit_highlightlang(self, node):
        lang = node.attributes["lang"].strip()
        if lang in self.jupyter_kernels:
            self.lang = lang
        else:
            self.warn(
                "Highlighting language({}) is not defined "
                "in jupyter_kernels in conf.py. "
                "Set kernel as default(python3)"
                .format(lang))
            self.lang = self.default_lang

    # =================
    # Inline elements
    # =================
    def visit_Text(self, node):
        text = node.astext()
        if self.in_code_block:
            self.code_lines.append(text)
            
    def depart_Text(self, node):
        pass

    # ================
    #  code blocks
    # ================
    def visit_literal_block(self, node):
        self.output_cell_type = JupyterOutputCellGenerators.GetGeneratorFromClasses(node.attributes['classes'])
        try:
            self.nodelang = node.attributes["language"].strip()
        except:
            self.nodelang = self.lang

        # Translate the language name across from the Sphinx to the Jupyter namespace
        self.nodelang = self.langTranslator.translate(self.nodelang)

        self.in_code_block = True
        self.code_lines = []

        # If the cell being processed contains code written in a language other than the one that
        # was specified as the default language, do not create a code block for it - turn it into
        # markup instead.
        if self.nodelang != self.langTranslator.translate(self.lang):
            self.output_cell_type = JupyterOutputCellGenerators.MARKDOWN


    def depart_literal_block(self, node):
        line_text = "".join(self.code_lines)
        formatted_line_text = self.strip_blank_lines_in_end_of_block(line_text)

        new_code_cell = self.output_cell_type.Generate(formatted_line_text, self)
        if self.output_cell_type is JupyterOutputCellGenerators.CODE_OUTPUT:
            # Output blocks must  be added to code cells to make any sense.
            # This script assumes that any output blocks will immediately follow a code
            # cell; a warning is raised if the cell immediately preceding this output
            # block is not a code cell.
            #
            # It is assumed that code cells may only have one output block - any more than
            # one will raise a warning and be ignored.
            mostRecentCell = self.output["cells"][-1]
            if mostRecentCell.cell_type != "code":
                self.warn("Warning: Class: output block found after a " + mostRecentCell.cell_type + " cell. Outputs may only come after code cells.")
            elif mostRecentCell.outputs:
                self.warn("Warning: Multiple class: output blocks found after a code cell. Each code cell may only be followed by either zero or one output blocks.")
            else:
                mostRecentCell.outputs.append(new_code_cell)
        else:
            self.output["cells"].append(new_code_cell)

        self.in_code_block = False


    # ===================
    #  general methods
    # ===================
    @staticmethod
    def strip_blank_lines_in_end_of_block(line_text):
        lines = line_text.split("\n")

        for line in range(len(lines)):
            if len(lines[-1].strip()) == 0:
                lines = lines[:-1]
            else:
                break

        return "\n".join(lines)


class JupyterTranslator(JupyterCodeTranslator):

    SPLIT_URI_ID_REGEX = re.compile(r"([^\#]*)\#?(.*)")

    def __init__(self, builder, document):
        super().__init__(builder, document)

        # Settings
        self.sep_lines = "  \n"
        self.sep_paras = "\n\n"
        self.indent_char = " "
        self.indent = self.indent_char * 4
        self.default_ext = ".ipynb"

        # Variables used in visit/depart
        self.in_code_block = False  # if False, it means in markdown_cell
        self.code_lines = []

        self.markdown_lines = []

        self.indents = []
        self.section_level = 0
        self.bullets = []
        self.list_item_starts = []
        self.in_topic = False
        self.reference_text_start = 0
        self.in_reference = False
        self.list_level = 0
        self.in_citation = False

    # specific visit and depart methods
    # ---------------------------------

    # ==============
    #  Sections
    # ==============
    def visit_document(self, node):
        """at start
        """
        JupyterCodeTranslator.visit_document(self, node)


    def depart_document(self, node):
        """at end

        Almost the exact same implementation as that of the superclass.
        """
        self.add_markdown_cell()
        JupyterCodeTranslator.depart_document(self, node)

    def visit_topic(self, node):
        self.in_topic = True

    def depart_topic(self, node):
        self.in_topic = False

    def visit_section(self, node):
        self.section_level += 1

    def depart_section(self, node):
        self.section_level -= 1


    #=================
    # Inline elements
    #=================
    def visit_Text(self, node):
        text = node.astext()

        if self.in_code_block:
            self.code_lines.append(text)
        else:
            self.markdown_lines.append(text)
            
    def depart_Text(self, node):
        pass

    # image
    def visit_image(self, node):
        uri = node.attributes["uri"]
        self.markdown_lines.append("![{0}]({0})".format(uri))

    # math
    def visit_math(self, node):
        """inline math"""
        math_text = node.attributes["latex"].strip()
        formatted_text = "$ {} $".format(math_text)
        self.markdown_lines.append(formatted_text)

    def visit_displaymath(self, node):
        """directive math"""
        math_text = node.attributes["latex"].strip()

        if self.list_level == 0:
            formatted_text = "$$\n{0}\n$${1}".format(
                math_text, self.sep_paras)
        else:
            formatted_text = "$$\n{0}\n$${1}".format(
                math_text, self.sep_paras)

        formatted_text = "<table width=100%><tr style='background-color: #FFFFFF !important;'><td width=75%>"\
                         + formatted_text\
                         + "</td><td width=25% style='text-align:center !important;'>"

        self.markdown_lines.append(formatted_text)

        # Add the line number reference.
        if node["ids"]:
            referenceBuilder = "(" + str(node["number"]) + ")"
            self.markdown_lines.append(referenceBuilder)

        self.markdown_lines.append("</td></tr></table>")

    def visit_raw(self, node):
        pass


    #==================
    #  markdown cells
    #==================

    # general paragraph
    def visit_paragraph(self, node):
        pass

    def depart_paragraph(self, node):
        if self.list_level > 0:
            self.markdown_lines.append(self.sep_lines)
        else:
            self.markdown_lines.append(self.sep_paras)

    # title(section)
    def visit_title(self, node):
        self.add_markdown_cell()

        if self.in_topic:
            self.markdown_lines.append(
                "{} ".format("#" * (self.section_level+1)))
        else:
            self.markdown_lines.append(
                "{} ".format("#" * self.section_level))

    def depart_title(self, node):
        self.markdown_lines.append(self.sep_paras)

    # emphasis(italic)
    def visit_emphasis(self, node):
        self.markdown_lines.append("*")

    def depart_emphasis(self, node):
        self.markdown_lines.append("*")

    # strong(bold)
    def visit_strong(self, node):
        self.markdown_lines.append("**")

    def depart_strong(self, node):
        self.markdown_lines.append("**")

    # figures
    def visit_figure(self, node):
        pass

    def depart_figure(self, node):
        self.markdown_lines.append(self.sep_lines)
    
    # reference
    def visit_reference(self, node):
        """anchor link"""
        self.in_reference = True
        self.markdown_lines.append("[")
        self.reference_text_start = len(self.markdown_lines)
        
    def depart_reference(self, node):
        if self.in_topic:
            # Jupyter Notebook uses the target text as its id
            uri_text = "".join(self.markdown_lines[self.reference_text_start:]).strip()
            uri_text = re.sub(
                self.URI_SPACE_REPLACE_FROM, self.URI_SPACE_REPLACE_TO, uri_text)
            formatted_text = "](#{})".format(uri_text)
            self.markdown_lines.append(formatted_text)
            
        else:
            # if refuri exists, then it includes id reference(#hoge)
            if "refuri" in node.attributes:
                refuri = node["refuri"]

                # add default extension(.ipynb)
                if "internal" in node.attributes and node.attributes["internal"] == True:
                    refuri = self.add_extension_to_inline_link(refuri, self.default_ext)
            else:
                # in-page link
                if "refid" in node:
                    refid = node["refid"]
                    refuri = "#{}".format(refid)
                # error
                else:
                    self.error("Invalid reference")
                    refuri = ""

            self.markdown_lines.append("]({})".format(refuri))
            
        self.in_reference = False

    # target: make anchor
    def visit_target(self, node):
        if "refid" in node.attributes:
            refid = node.attributes["refid"]
            self.markdown_lines.append(
                "\n<a id='{}'></a>\n".format(refid))

    # list items
    def visit_bullet_list(self, node):
        self.list_level += 1
        # markdown does not have option changing bullet chars
        self.bullets.append("-")
        self.indents.append(len(self.bullets[-1])+1)
        
    def depart_bullet_list(self, node):
        self.list_level -= 1
        if self.list_level == 0:
            self.markdown_lines.append(self.sep_paras)
            if self.in_topic:
                self.add_markdown_cell()
        
        self.bullets.pop()
        self.indents.pop()

    def visit_enumerated_list(self, node):
        self.list_level += 1
        # markdown does not have option changing bullet chars
        self.bullets.append("1.")
        self.indents.append(len(self.bullets[-1])+1)

    def depart_enumerated_list(self, node):
        self.list_level -= 1
        if self.list_level == 0:
            self.markdown_lines.append(self.sep_paras)

        self.bullets.pop()
        self.indents.pop()

    def visit_list_item(self, node):
        # self.first_line_in_list_item = True
        head = "{} ".format(self.bullets[-1])
        self.markdown_lines.append(head)
        self.list_item_starts.append(len(self.markdown_lines))

    def depart_list_item(self, node):
        # self.first_line_in_list_item = False

        list_item_start = self.list_item_starts.pop()
        indent = self.indent_char * self.indents[-1]
        br_removed_flag = False
        
        # remove last breakline
        if self.markdown_lines[-1][-1] == "\n":
            br_removed_flag = True
            self.markdown_lines[-1] = self.markdown_lines[-1][:-1]

        for i in range(list_item_start, len(self.markdown_lines)):
            self.markdown_lines[i] = self.markdown_lines[i].replace(
                "\n", "\n{}".format(indent))

        # add breakline
        if br_removed_flag:
            self.markdown_lines.append("\n")

    # definition list
    def visit_definition_list(self, node):
        self.markdown_lines.append("\n<dl style='margin: 20px 0;'>\n")

    def depart_definition_list(self, node):
        self.markdown_lines.append("\n</dl>{}".format(self.sep_paras))

    def visit_term(self, node):
        self.markdown_lines.append("<dt>")

    def depart_term(self, node):
        self.markdown_lines.append("</dt>\n")

    def visit_definition(self, node):
        self.markdown_lines.append("<dd>\n")

    def depart_definition(self, node):
        self.markdown_lines.append("</dd>\n")

    # field list
    def visit_field_list(self, node):
        self.visit_definition_list(node)

    def depart_field_list(self, node):
        self.depart_definition_list(node)

    def visit_field_name(self, node):
        self.visit_term(node)

    def depart_field_name(self, node):
        self.depart_term(node)

    def visit_field_body(self, node):
        self.visit_definition(node)

    def depart_field_body(self, node):
        self.depart_definition(node)

    # citation
    def visit_citation(self, node):
        self.in_citation = True
        if "ids" in node.attributes:
            ids = node.attributes["ids"]
            id_text = ""
            for id_ in ids:
                id_text += "{} ".format(id_)
            else:
                id_text = id_text[:-1]

            self.markdown_lines.append(
                "<a id='{}'></a>\n".format(id_text))

    def depart_citation(self, node):
        self.in_citation = False

    # label
    def visit_label(self, node):
        if self.in_citation:
            self.markdown_lines.append("\[")

    def depart_label(self, node):
        if self.in_citation:
            self.markdown_lines.append("\] ")

    # ================
    #  code blocks are implemented in the superclass.
    # ================
    def visit_literal_block(self, node):
        JupyterCodeTranslator.visit_literal_block(self, node)

        if self.in_code_block:
            self.add_markdown_cell()


    # ===================
    #  general methods
    # ===================
    def add_markdown_cell(self):
        """split a markdown cell here

        * append `markdown_lines` to notebook
        * reset `markdown_lines`
        """
        line_text = "".join(self.markdown_lines)
        formatted_line_text = self.strip_blank_lines_in_end_of_block(line_text)

        if len(formatted_line_text.strip()) > 0:
            new_md_cell = nbformat.v4.new_markdown_cell(formatted_line_text)
            self.output["cells"].append(new_md_cell)
            self.markdown_lines = []

    @classmethod
    def split_uri_id(cls, uri):
        return re.search(cls.SPLIT_URI_ID_REGEX, uri).groups()

    @classmethod
    def add_extension_to_inline_link(cls, uri, ext):
        if "." not in uri:
            uri, id_ = cls.split_uri_id(uri)
            return "{}{}#{}".format(uri, ext, id_)

        return uri


class JupyterBuilder(Builder):
    """
    Builds Jupyter Notebook
    """
    name = "jupyter"
    format = "ipynb"
    out_suffix = ".ipynb"
    allow_parallel = True

    _writer_class = JupyterWriter

    def init(self):
        # settings
        pass

    def get_outdated_docs(self):
        for docname in self.env.found_docs:
            if docname not in self.env.all_docs:
                yield docname
                continue
            targetname = self.env.doc2path(docname, self.outdir,
                                           self.out_suffix)
            try:
                targetmtime = os.path.getmtime(targetname)
            except Exception:
                targetmtime = 0
            try:
                srcmtime = os.path.getmtime(self.env.doc2path(docname))
                if srcmtime > targetmtime:
                    yield docname
            except EnvironmentError:
                pass

    def get_target_uri(self, docname, typ=None):
        return docname

    def prepare_writing(self, docnames):
        self.writer = self._writer_class(self)

    def write_doc(self, docname, doctree):
        # work around multiple string % tuple issues in docutils;
        # replace tuples in attribute values with lists
        doctree = doctree.deepcopy()
        destination = docutils.io.StringOutput(encoding="utf-8")
        self.writer.write(doctree, destination)
        outfilename = os.path.join(
            self.outdir, os_path(docname) + self.out_suffix)

        # mkdir if the directory does not exist
        ensuredir(os.path.dirname(outfilename))

        try:
            with codecs.open(outfilename, "w", "utf-8") as f:
                f.write(self.writer.output)
        except (IOError, OSError) as err:
            self.warn("error writing file %s: %s" % (outfilename, err))

    def copy_static_files(self):
        # copy all static files
        self.info(bold("copying static files... "), nonl=True)
        ensuredir(os.path.join(self.outdir, '_static'))

        # excluded = Matcher(self.config.exclude_patterns + ["**/.*"])
        for static_path in self.config["jupyter_static_file_path"]:
            entry = os.path.join(self.confdir, static_path)
            if not os.path.exists(entry):
                self.warn(
                    "jupyter_static_path entry {} does not exist"
                    .format(entry))
            else:
                copy_asset(entry, os.path.join(self.outdir, "_static"))
        self.info("done")

    def finish(self):
        self.finish_tasks.add_task(self.copy_static_files)


def get_source_file_name(filepath, srcdir):
    file_path_list = filepath.split("/")
    srcdir_path_list = srcdir.split("/")

    for i in range(len(srcdir_path_list)):
        if srcdir_path_list[i] != file_path_list[i]:
            raise ValueError("File path does not exist in the source directory")
    
    file_name_list = file_path_list[len(srcdir_path_list)-1:]
    return "/".join(file_name_list)


def setup(app):
    app.add_builder(JupyterBuilder)
    app.add_config_value("jupyter_kernels", None, "jupyter")
    app.add_config_value("jupyter_headers", None, "jupyter")
    app.add_config_value("jupyter_conversion_mode", None, "jupyter")
    app.add_config_value("jupyter_write_metadata", True, "jupyter")
    app.add_config_value("jupyter_static_file_path", [], "jupyter")
    app.add_config_value("jupyter_welcome_block", None, "jupyter")

    return {
        "version": "0.0.1",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
    }
