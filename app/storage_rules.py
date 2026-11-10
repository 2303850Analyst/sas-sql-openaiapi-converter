"""User-approved target placement: unqualified/WORK -> session temporary."""
from sqlglot import exp

def target_tree(tree):
    tree=tree.copy()
    output=tree.this if isinstance(tree,exp.Create) else None
    temporary=output is not None and not output.catalog and (not output.db or output.db.casefold()=='work')
    for table in tree.find_all(exp.Table):
        if not table.catalog and table.db.casefold()=='work':table.set('db',None)
    if temporary:
        properties=tree.args.get('properties') or exp.Properties(expressions=[])
        if not properties.find(exp.TemporaryProperty):properties.append('expressions',exp.TemporaryProperty())
        tree.set('properties',properties)
    return tree
