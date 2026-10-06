import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('public_reproduce', ROOT/'tools/reproduce.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class PublicReproductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'candidate'
        self.root.mkdir()
        for name in ('src', 'configs', 'tests', 'tools', 'results'):
            shutil.copytree(ROOT/name, self.root/name, ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('.gitignore', '.gitattributes', 'pyproject.toml'):
            shutil.copyfile(ROOT/name, self.root/name)

    def cli(self, model='dispatch', mode='smoke', order='forward', output='outputs/new'):
        return subprocess.run(['python3','-B','tools/reproduce.py','--model',model,'--mode',mode,
                               '--order',order,'--output',output],cwd=self.root,capture_output=True,text=True)

    def rejected(self, result, reason):
        self.assertNotEqual(result.returncode,0)
        self.assertIn(reason,result.stderr)

    def test_success_both_models_and_receipt(self):
        for model in ('dispatch','cy'):
            result=self.cli(model=model,output='outputs/'+model)
            self.assertEqual(result.returncode,0,result.stderr)
            receipt=json.loads((self.root/'outputs'/model/'receipt.json').read_text())
            self.assertEqual(receipt['status'],'passed')
            self.assertTrue(receipt['hash_match'])
            self.assertEqual((receipt['valid_seed_count'],receipt['coalition_count']),(1,8))
            self.assertTrue(all(Path(v).is_relative_to(self.root/'src') for v in receipt['import_paths'].values()))

    def test_source_tampering(self):
        with (self.root/'src/socfai_incentive/value.py').open('a') as f:f.write('\n# changed\n')
        self.rejected(self.cli(),'file integrity mismatch')
        self.assertFalse((self.root/'outputs/new').exists())

    def test_config_tampering(self):
        with (self.root/'configs/smoke.json').open('a') as f:f.write(' ')
        self.rejected(self.cli(),'file integrity mismatch')

    def test_reference_hash_tampering(self):
        path=self.root/'results/reference-manifest.json';data=json.loads(path.read_text())
        data['references'][0]['scientific_sha256']='0'*64;path.write_text(json.dumps(data))
        self.rejected(self.cli(),'manifest hash mismatch')

    def test_source_manifest_tampering(self):
        (self.root/'results/source-manifest.json').write_text('{}')
        self.rejected(self.cli(),'manifest hash mismatch')

    def test_reference_asset_tampering(self):
        (self.root/'results/dispatch/summary.json').write_text('{}')
        self.rejected(self.cli(),'file integrity mismatch')

    def test_unlisted_source(self):
        (self.root/'src/socfai_incentive/extra.py').write_text('x=1')
        self.rejected(self.cli(),'inventory mismatch')

    def test_unlisted_bytecode(self):
        (self.root/'src/socfai_incentive/extra.pyc').write_bytes(b'bad')
        self.rejected(self.cli(),'inventory mismatch')

    def test_source_symlink(self):
        path=self.root/'configs/smoke.json';outside=Path(self.tmp.name)/'config.json'
        path.rename(outside);path.symlink_to(outside)
        self.rejected(self.cli(),'nonlocal or symlink input')

    def test_existing_output_preserved(self):
        path=self.root/'outputs/new';path.mkdir(parents=True);(path/'keep').write_bytes(b'keep')
        self.rejected(self.cli(),'output exists')
        self.assertEqual((path/'keep').read_bytes(),b'keep')

    def test_existing_empty_output(self):
        (self.root/'outputs/new').mkdir(parents=True)
        self.rejected(self.cli(),'output exists')

    def test_output_escape(self):
        for output in ('../outside','outputs/../../outside','outputs'):
            self.rejected(self.cli(output=output),'under repository outputs')

    def test_output_symlink_escape(self):
        (self.root/'outputs').symlink_to(Path(self.tmp.name),target_is_directory=True)
        self.rejected(self.cli(),'under repository outputs')

    def test_output_symlink_inside_root(self):
        (self.root/'outputs/real').mkdir(parents=True)
        (self.root/'outputs/link').symlink_to(self.root/'outputs/real',target_is_directory=True)
        self.rejected(self.cli(output='outputs/link/new'),'symlink output')

    def test_invalid_choices(self):
        for kwargs in ({'model':'wrong'},{'mode':'wrong'},{'order':'wrong'}):
            self.rejected(self.cli(**kwargs),'invalid choice')

    def fixture(self):
        result=self.cli();self.assertEqual(result.returncode,0,result.stderr)
        work=self.root/'outputs/new/run'
        return json.loads((work/'scientific.json').read_bytes()),work,{
            'seeds':[0],'coalition_count':8,'scientific_sha256':tool.sha((work/'scientific.json').read_bytes())}

    def test_invalid_seed(self):
        result,work,ref=self.fixture();result['coalition_results'][0]['seed']=100
        with self.assertRaisesRegex(ValueError,'invalid seed'):tool.validate_result('dispatch','smoke',result,work,ref)

    def test_incomplete_result(self):
        result,work,ref=self.fixture();result['coalition_results'].pop()
        with self.assertRaisesRegex(ValueError,'incomplete'):tool.validate_result('dispatch','smoke',result,work,ref)

    def test_failed_seed(self):
        result,work,ref=self.fixture();result['coalition_results'][0]['status']='failed'
        with self.assertRaisesRegex(ValueError,'invalid seed result'):tool.validate_result('dispatch','smoke',result,work,ref)

    def test_scientific_hash_mismatch(self):
        result,work,ref=self.fixture();ref['scientific_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'hash mismatch'):tool.validate_result('dispatch','smoke',result,work,ref)

    def test_incomplete_output_package(self):
        result,work,ref=self.fixture();(work/'rewards.csv').unlink()
        with self.assertRaisesRegex(ValueError,'incomplete output'):tool.validate_result('dispatch','smoke',result,work,ref)

    def test_foreign_import_rejected(self):
        class Foreign:
            __file__='/outside/value.py'
        with patch.dict(tool.sys.modules,{'socfai_incentive.foreign':Foreign()}):
            with self.assertRaisesRegex(ValueError,'import outside'):tool.check_imports(json.loads((ROOT/'results/source-manifest.json').read_text()))

    def test_execution_failure_is_nonzero_and_receipt_is_failed(self):
        # Use the approved actual package, inject only an execution failure, and retain the receipt.
        with tempfile.TemporaryDirectory(dir=ROOT/'outputs' if (ROOT/'outputs').exists() else ROOT) as tmp:
            # A separate output name is required; TemporaryDirectory itself must not be used.
            dest=ROOT/'outputs'/('failure-'+Path(tmp).name)
            self.addCleanup(shutil.rmtree,dest,True)
            args=['reproduce.py','--model','dispatch','--mode','smoke','--order','forward','--output',str(dest)]
            from socfai_incentive import runner
            with patch.object(runner,'run_seed',side_effect=ValueError('injected invalid seed')),patch.object(tool.sys,'argv',args),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(tool.main(),1)
            receipt=json.loads((dest/'receipt.json').read_text())
            self.assertEqual(receipt['status'],'failed')
            self.assertFalse(receipt['hash_match'])


if __name__=='__main__':
    unittest.main()
